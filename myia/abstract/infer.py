"""Algorithms for inference."""

import asyncio
import typing
from dataclasses import is_dataclass, replace as dc_replace
from functools import reduce

import numpy as np

from .. import dtype, operations
from ..info import About
from ..ir import Graph, MetaGraph
from ..prim import Primitive, ops as P
from ..utils import (
    ADT,
    InferenceError,
    MyiaTypeError,
    Overload,
    Partializable,
    SymbolicKeyInstance,
    dataclass_fields,
    dataclass_methods,
    infer_trace,
    is_dataclass_type,
    overload,
    type_error_nargs,
)
from .data import (
    ALIASID,
    ANYTHING,
    DATA,
    SHAPE,
    TYPE,
    VALUE,
    AbstractADT,
    AbstractArray,
    AbstractClass,
    AbstractClassBase,
    AbstractDict,
    AbstractError,
    AbstractExternal,
    AbstractFunction,
    AbstractJTagged,
    AbstractKeywordArgument,
    AbstractScalar,
    AbstractTuple,
    AbstractType,
    AbstractValue,
    DummyFunction,
    Function,
    GraphFunction,
    JTransformedFunction,
    Macro,
    MacroFunction,
    MetaGraphFunction,
    PartialApplication,
    PrimitiveFunction,
    TypedPrimitive,
    VirtualFunction,
    empty,
    listof,
)
from .loop import InferenceLoop, Pending, force_pending
from .ref import Context, EvaluationCache, Reference, VirtualReference
from .utils import (
    amerge,
    bind,
    broaden as _broaden,
    concretize_abstract,
    normalize_adt,
    sensitivity_transform,
    type_to_abstract,
)


class ArrayWrapper:
    """Wrap array so that it remains on accelerator device.

    Attributes:
        array: The array that is inside ArrayWrapper.
        dtype: The dtype of the array that is inside ArrayWrapper.
        shape: The shape of the array that is inside ArrayWrapper.

    """

    def __init__(self, array, dtype, shape, backend):
        """Initialize the ArrayWrapper."""
        self.array = array
        self.dtype = dtype
        self.shape = shape
        self.backend = backend


class InferenceEngine:
    """Infer various properties about nodes in graphs.

    Attributes:
        pipeline: The Pipeline we are running.
        constructors: As an argument to __init__, a map from primitives
            to inferrer classes, which will be instantiated automatically
            by the InferenceEngine.
        context_class: The class to use to instantiate contexts.

    """

    def __init__(self,
                 pipeline,
                 *,
                 constructors,
                 context_class=Context):
        """Initialize the InferenceEngine."""
        self.loop = InferenceLoop(InferenceError)
        self.pipeline = pipeline
        self.mng = self.pipeline.resources.manager
        self._constructors = constructors
        self.errors = []
        self.context_class = context_class
        self.reset()

    def reset(self):
        """Reset all of the InferenceEngine's caches."""
        self.cache = EvaluationCache(
            loop=self.loop,
            keycalc=self.compute_ref,
            keytransform=self.get_actual_ref
        )
        self.reference_map = {}
        self.constructors = {
            prim: cons()
            for prim, cons in self._constructors.items()
        }

    def run(self, graph, *, argspec, outspec=None):
        """Run the inferrer on a graph given initial values.

        Arguments:
            graph: The graph to analyze.
            argspec: The arguments. Must be a tuple of AbstractValue.
            outspec (optional): Expected inference result. If provided,
                inference result will be checked against it.
        """
        assert not isinstance(outspec, dict)
        argrefs = [VirtualReference(arg) for arg in argspec]

        self.mng.add_graph(graph)
        empty_context = self.context_class.empty()
        root_context = empty_context.add(graph, argspec)
        output_ref = self.ref(graph.return_, root_context)

        async def _run():
            inf = GraphInferrer(graph, empty_context)
            self.loop.schedule(
                execute_inferrers(self, [inf], None, argrefs)
            )

        async def _check():
            amerge(concretize_abstract(await output_ref.get()),
                   outspec,
                   forced=False)

        self.run_coroutine(_run())
        if outspec is not None:
            self.run_coroutine(_check())

        return concretize_abstract(output_ref.get_sync()), root_context

    def ref(self, node, context):
        """Return a Reference to the node in the given context."""
        if node.is_constant_graph():
            context = context.filter(node.value.parent)
        else:
            context = context.filter(node.graph)
        return Reference(self, node, context)

    async def compute_ref(self, ref):
        """Compute the value associated to the Reference."""
        node = ref.node

        if node.is_constant():
            inferred = ref.node.abstract
            if (inferred is not None
                    and not isinstance(inferred, AbstractFunction)):
                return inferred
            return await self.infer_constant(ref)

        elif node.is_apply():
            return await self.infer_apply(ref)

        else:
            raise AssertionError(f'Missing information for {ref.node}', ref)

    def get_inferred(self, ref):
        """Get a Future for the value associated to the Reference.

        Results are cached.
        """
        return self.cache.get(ref)

    async def reroute(self, orig, new):
        """Set the inference result for orig to the result for new.

        This sets an entry in reference_map from orig to new.
        """
        if not new.node.debug.about:
            # This will link the old node's debug info to the new node, if
            # necessary.
            new.node.debug.about = About(orig.node.debug, 'reroute')
        self.reference_map[orig] = new
        return await self.get_inferred(new)

    def get_actual_ref(self, ref):
        """Return the replacement reference for ref, or ref itself."""
        while ref in self.reference_map:
            ref = self.reference_map[ref]
        return ref

    def run_coroutine(self, coro, throw=True):
        """Run an async function using this inferrer's loop."""
        errs_before = len(self.errors)
        try:
            fut = self.loop.schedule(coro)
            self.loop.run_forever()
            self.errors.extend(self.loop.collect_errors())
            for err in self.errors[errs_before:]:
                err.engine = self
            if errs_before < len(self.errors):
                if throw:  # pragma: no cover
                    for err in self.errors:
                        if isinstance(err, InferenceError):
                            raise err
                    else:
                        raise err
                else:
                    return None  # pragma: no cover
            return fut.result()
        finally:
            for task in asyncio.all_tasks(self.loop):
                task._log_destroy_pending = False

    get_inferrer_for = Overload()

    @get_inferrer_for.wrapper
    def get_inferrer_for(__call__, self, fn):
        """Return the Inferrer for the given function."""
        tracking = getattr(fn, 'tracking_id', None)
        if tracking is None:
            return __call__(self, fn)
        if fn not in self.constructors:
            fn_generic = dc_replace(fn, tracking_id=None)
            inf = __call__(self, fn_generic)
            self.constructors[fn] = TrackedInferrer(inf)
        return self.constructors[fn]

    @get_inferrer_for.register
    def get_inferrer_for(self, pf: PrimitiveFunction):
        return self.constructors[pf.prim]

    @get_inferrer_for.register
    def get_inferrer_for(self, g: GraphFunction):
        if g not in self.constructors:
            self.constructors[g] = GraphInferrer(g.graph, g.context)
        return self.constructors[g]

    @get_inferrer_for.register
    def get_inferrer_for(self, part: PartialApplication):
        return PartialInferrer(
            self.get_inferrer_for(part.fn),
            part.args
        )

    @get_inferrer_for.register
    def get_inferrer_for(self, j: JTransformedFunction):
        return JInferrer(
            self.get_inferrer_for(j.fn),
            j.fn
        )

    @get_inferrer_for.register
    def get_inferrer_for(self, vf: (VirtualFunction, TypedPrimitive)):
        return VirtualInferrer(
            vf.args,
            vf.output
        )

    @get_inferrer_for.register
    def get_inferrer_for(self, df: DummyFunction):
        raise MyiaTypeError(f'Trying to call dummy')

    @get_inferrer_for.register
    def get_inferrer_for(self, mg: MetaGraphFunction):
        if mg not in self.constructors:
            self.constructors[mg] = GraphInferrer(mg.metagraph, None)
        return self.constructors[mg]

    @get_inferrer_for.register
    def get_inferrer_for(self, m: MacroFunction):
        if m not in self.constructors:
            self.constructors[m] = MacroInferrer(m.macro)
        return self.constructors[m]

    async def execute(self, fn, *args):
        """Infer the result of fn(*args)."""
        infs = [self.get_inferrer_for(poss)
                for poss in await fn.get()]
        argrefs = [VirtualReference(a) for a in args]
        return await execute_inferrers(self, infs, None, argrefs)

    async def infer_apply(self, ref):
        """Infer the type of a ref of an Apply node."""
        ctx = ref.context
        n_fn, *n_args = ref.node.inputs
        # We await on the function node to get the inferrer
        fn_ref = self.ref(n_fn, ctx)
        fn = await fn_ref.get()
        argrefs = [self.ref(node, ctx) for node in n_args]

        if isinstance(fn, AbstractType):
            g = ref.node.graph
            newfn = g.apply(P.partial, P.make_record, fn.values[VALUE])
            newcall = g.apply(newfn, *n_args)
            return await self.reroute(ref, self.ref(newcall, ctx))

        elif isinstance(fn, AbstractError):
            raise MyiaTypeError(
                f'Trying to call a function with type '
                f'{fn.values[VALUE]} {fn.values[DATA] or ""}.'
            )

        elif isinstance(fn, AbstractClassBase):
            g = ref.node.graph
            newfn = g.apply(operations.getattr, fn_ref.node, '__call__')
            newcall = g.apply(newfn, *n_args)
            return await self.reroute(ref, self.ref(newcall, ctx))

        elif not isinstance(fn, AbstractFunction):
            raise MyiaTypeError(f'Myia does not know how to call {fn}')

        infs = [self.get_inferrer_for(poss)
                for poss in await fn.get()]

        return await self.loop.schedule(
            execute_inferrers(self, infs, ref, argrefs),
            context_map={
                infer_trace: {**infer_trace.get(), ctx: ref}
            }
        )

    async def infer_constant(self, ctref):
        """Infer the type of a ref of a Constant node."""
        v = self.pipeline.resources.convert(ctref.node.value)
        return to_abstract(
            v,
            context=ctref.context,
            node=ctref.node,
            loop=self.loop
        )

    def abstract_merge(self, *values):
        """Merge a list of AbstractValues together."""
        return reduce(amerge, values)

    def check_predicate(self, predicate, x):
        """Returns whether the predicate applies on x.

        A predicate can be:
            * A Myia type (dtype.Int[64] etc.)
            * A Python class
            * A callable that returns a boolean
        """
        if isinstance(predicate, dtype.TypeMeta):
            return issubclass(x, predicate)
        elif isinstance(predicate, type):
            return isinstance(x, predicate)
        elif callable(predicate):
            return predicate(self, x)
        else:
            raise ValueError(predicate)  # pragma: no cover

    def assert_predicate(self, predicate, x):
        """Check that the predicate applies, raise error if not."""
        if not self.check_predicate(predicate, x):
            raise MyiaTypeError(f'Expected {predicate}, not {x}')

    def check(self, predicate, *values):
        """Merge all values and check that the predicate applies.

        Some values may be Pending, in which case a check will be
        scheduled when they are finally resolved.
        """
        for value in values:
            if isinstance(value, Pending):
                value.add_done_callback(
                    lambda fut: self.assert_predicate(
                        predicate, fut.result()
                    )
                )
            else:
                self.assert_predicate(predicate, value)
        return self.abstract_merge(*values)

    async def check_immediate(self, predicate, *values):
        """Merge values, check predicate, and return result.

        Unlike check, if the result is Pending, it will be resolved
        immediately.
        """
        return await force_pending(self.check(predicate, *values))


_number_types = [
    dtype.Int[8], dtype.Int[16], dtype.Int[32], dtype.Int[64],
    dtype.UInt[8], dtype.UInt[16], dtype.UInt[32], dtype.UInt[64],
    dtype.Float[16], dtype.Float[32], dtype.Float[64],
]


def from_value(v, broaden=False, **kwargs):
    """Convert a value to an abstract value.

    Arguments:
        v: The value to convert.
        broaden: If True, concrete values will be made more abstract, so e.g.
            the value 1234 would become ANYTHING.
    """
    a = to_abstract(v, **kwargs)
    if broaden:
        a = _broaden(a)
    return a


@overload.wrapper(bootstrap=True)
def to_abstract(fn, self, v, **kwargs):
    """Translate the value to an abstract value.

    Arguments:
        v: The value to convert.
        context: The context in which the value was found, used if the value
            is a Graph.
        node: The node for the Constant we are converting, if there is one,
            so that we can generate a tracking_id.
        loop: The InferenceLoop, or None. If not None, scalars ints or floats
            will be given a Pending type so that it can adapt to the types of
            the variables they interact with.
    """
    if fn is not None:
        rval = fn(self, v, **kwargs)

    elif is_dataclass_type(v):
        return AbstractType(type_to_abstract(v))

    elif is_dataclass(v):
        assert not isinstance(v, Function)
        new_args = {}
        for name, value in dataclass_fields(v).items():
            new_args[name] = self(value, **kwargs)
        methods = dataclass_methods(type(v))
        rval = AbstractClass(type(v), new_args, methods)

    elif isinstance(v, dtype.TypeMeta):
        rval = AbstractType(v)

    else:
        try:
            typ = dtype.pytype_to_myiatype(type(v))
        except KeyError:
            rval = AbstractExternal({
                VALUE: v,
                TYPE: type(v),
            })
        else:
            assert issubclass(typ, (dtype.External, dtype.EnvType))
            rval = AbstractScalar({
                VALUE: v,
                TYPE: typ,
            })

    return rval


@overload  # noqa: F811
def to_abstract(self, v: AbstractValue, **kwargs):
    return AbstractType(v)


@overload  # noqa: F811
def to_abstract(self, v: Graph, context=None, node=None, **kwargs):
    ctx = context or Context.empty()
    return AbstractFunction(
        GraphFunction(v, ctx, tracking_id=node)
    )


@overload  # noqa: F811
def to_abstract(self, v: MetaGraph, node=None, **kwargs):
    return AbstractFunction(
        MetaGraphFunction(v, Context.empty(), tracking_id=node)
    )


@overload  # noqa: F811
def to_abstract(self, v: Macro, **kwargs):
    return AbstractFunction(MacroFunction(v))


@overload  # noqa: F811
def to_abstract(self, v: Primitive, node=None, **kwargs):
    return AbstractFunction(PrimitiveFunction(v, tracking_id=node))


@overload  # noqa: F811
def to_abstract(self, v: SymbolicKeyInstance, **kwargs):
    return AbstractScalar({VALUE: v, TYPE: dtype.SymbolicKeyType})


@overload  # noqa: F811
def to_abstract(self, v: (bool, type(None)), **kwargs):
    typ = dtype.pytype_to_myiatype(type(v))
    return AbstractScalar({
        VALUE: v,
        TYPE: typ,
    })


@overload  # noqa: F811
def to_abstract(self, v: (int, float, np.integer, np.floating),
                loop=None, **kwargs):
    typ = dtype.pytype_to_myiatype(type(v))
    if loop is not None:
        prio = 1 if issubclass(typ, dtype.Float) else 0
        typ = loop.create_pending_from_list(
            _number_types, typ, lambda: prio
        )
    return AbstractScalar({
        VALUE: v,
        TYPE: typ,
    })


@overload  # noqa: F811
def to_abstract(self, v: tuple, **kwargs):
    return AbstractTuple([self(elem, **kwargs)
                          for elem in v])


@overload  # noqa: F811
def to_abstract(self, v: list, **kwargs):
    if v == []:
        return empty
    else:
        elem_types = [self(elem, **kwargs) for elem in v]
        elem_type = reduce(amerge, elem_types)
        return listof(_broaden(elem_type))


@overload  # noqa: F811
def to_abstract(self, v: dict, **kwargs):
    entries = dict((k, self(val, **kwargs)) for k, val in v.items())
    return AbstractDict(entries)


@overload  # noqa: F811
def to_abstract(self, v: np.ndarray, alias_map={}, **kwargs):
    tracks = {SHAPE: v.shape}
    if id(v) in alias_map:
        tracks[ALIASID] = alias_map[id(v)]
    return AbstractArray(
        AbstractScalar({
            VALUE: ANYTHING,
            TYPE: dtype.np_dtype_to_type(str(v.dtype)),
        }),
        tracks
    )


@overload  # noqa: F811
def to_abstract(self, v: ArrayWrapper, **kwargs):
    return AbstractArray(
        AbstractScalar({
            VALUE: ANYTHING,
            TYPE: dtype.np_dtype_to_type(str(v.dtype)),
        }),
        {SHAPE: v.shape}
    )


@overload  # noqa: F811
def to_abstract(self, v: typing._GenericAlias, **kwargs):
    return AbstractType(type_to_abstract(v))


@overload  # noqa: F811
def to_abstract(self, v: ADT, **kwargs):
    new_args = {}
    for name, value in dataclass_fields(v).items():
        new_args[name] = self(value, **kwargs)
    draft = AbstractADT(type(v), new_args, dataclass_methods(type(v)))
    return normalize_adt(draft)


class Inferrer(Partializable):
    """Infer the result of a function.

    Attributes:
        cache: Map tuples of abstract values to an abstract result.

    """

    def __init__(self):
        """Initialize the Inferrer."""
        self.cache = {}

    def nokw(self, args):
        """Assert that there are no keyword arguments."""
        for arg in args:
            if isinstance(arg, AbstractKeywordArgument):
                raise MyiaTypeError('Keyword arguments are not allowed here')

    async def normalize_args(self, args):
        """Return normalized versions of the arguments.

        By default, this returns args unchanged.
        """
        self.nokw(args)
        return self.normalize_args_sync(args)

    def normalize_args_sync(self, args):
        """Return normalized versions of the arguments.

        By default, this returns args unchanged.
        """
        return args

    async def reroute(self, engine, outref, argrefs):
        """Return a replacement node to infer from instead of this one."""
        return None

    async def run(self, engine, outref, argrefs):
        """Run inference.

        This typically calls the infer method on the abstract values
        and caches the result. Some specific operations may work with
        the References directly.

        Arguments:
            engine: The InferenceEngine
            outref: A Reference to the output (could be None)
            argrefs: A tuple of References to the arguments
        """
        unnorm_args = tuple([await ref.get() for ref in argrefs])
        args = await self.normalize_args(unnorm_args)
        if args not in self.cache:
            self.cache[args] = await self.infer(engine, *args)
        return self.cache[args]

    async def infer(self, engine, *args):
        """Run inference on a tuple of abstract arguments."""
        raise NotImplementedError()

    def __repr__(self):
        return f'{type(self)}'


class TrackedInferrer(Inferrer):
    """Wrap another inferrer to track a subset of uses.

    A TrackedInferrer has its own cache that maps possible calls to
    their results, but is ultimately backed by a different inferrer.
    Multiple TrackedInferrers can be backed by the same Inferrer.

    Attributes:
        subinf: Inferrer to use.

    """

    def __init__(self, subinf):
        """Initialize the TrackedInferrer."""
        super().__init__()
        self.subinf = subinf

    async def reroute(self, engine, outref, argrefs):
        """Return a replacement node to infer from instead of this one."""
        return await self.subinf.reroute(engine, outref, argrefs)

    async def run(self, engine, outref, argrefs):
        """Run the inference."""
        args = tuple([await ref.get() for ref in argrefs])
        args = await self.subinf.normalize_args(args)
        self.cache[args] = await self.subinf.run(engine, outref, argrefs)
        return self.cache[args]


class MacroInferrer(Inferrer):
    """Inferrer for Macros."""

    def __init__(self, macro):
        """Initialize a MacroInferrer."""
        super().__init__()
        self.macro = macro

    async def reroute(self, engine, outref, argrefs):
        """Apply the macro."""
        return await self.macro.reroute(engine, outref, argrefs)


class GraphInferrer(Inferrer):
    """Base Inferrer for Graph and MetaGraph.

    Attributes:
        context: The context in which the Graph/MetaGraph is.

    """

    def __init__(self, graph, context):
        """Initialize a GraphInferrer."""
        super().__init__()
        self._graph = graph
        if context is not None:
            self.context = context.filter(graph and graph.parent)
        else:
            self.context = Context.empty()
        self.graph_cache = {}

    async def normalize_args(self, args):
        """Return normalized versions of the arguments."""
        return await self._graph.normalize_args(args)

    def normalize_args_sync(self, args):
        """Return normalized versions of the arguments."""
        return self._graph.normalize_args_sync(args)

    def get_graph(self, engine, args):
        """Generate the graph for the given args."""
        sig = self._graph.make_signature(args)
        if sig not in self.graph_cache:
            g = self._graph.generate_graph(sig)
            g = engine.pipeline.resources.convert(g)
            self.graph_cache[sig] = g
        return self.graph_cache[sig]

    def make_context(self, engine, args):
        """Create a Context object using the given args."""
        args = self.normalize_args_sync(args)
        _, ctx = self._make_argkey_and_context(engine, args)
        return ctx

    def _make_argkey_and_context(self, engine, argvals):
        assert argvals is not None
        g = self.get_graph(engine, argvals)
        argkey = tuple(argvals)
        # Update current context using the fetched properties.
        return argkey, self.context.add(g, argkey)

    async def infer(self, engine, *args):
        """Infer the abstract result given the abstract arguments."""
        g = self.get_graph(engine, args)
        nargs = len(g.parameters)

        if len(args) != nargs:
            raise type_error_nargs(self, nargs, len(args))

        argkey, context = self._make_argkey_and_context(engine, args)

        # We associate each parameter of the Graph with its value for each
        # property, in the context we built.
        for p, arg in zip(g.parameters, argkey):
            ref = engine.ref(p, context)
            engine.cache.set_value(ref, arg)

        out = engine.ref(g.return_, context)
        return await engine.get_inferred(out)


class PartialInferrer(Inferrer):
    """Inferrer for partial application.

    Attributes:
        fn: The Inferrer to use for the full list of arguments.
        args: The partial arguments.

    """

    def __init__(self, fn, args):
        """Initialize a PartialInferrer."""
        super().__init__()
        self.fn = fn
        self.args = args

    async def reroute(self, engine, outref, argrefs):
        """Reroute partial(f, ...)(...) to f(..., ...)."""
        ctx = outref.context
        fn, *args = outref.node.inputs
        collapse = False
        while True:
            fn = engine.get_actual_ref(engine.ref(fn, ctx)).node
            if fn.is_apply():
                fnfn = await engine.ref(fn.inputs[0], ctx).get()
                if isinstance(fnfn, AbstractFunction):
                    poss = await fnfn.get()
                    if len(poss) == 1:
                        prim, = poss
                        if (isinstance(prim, PrimitiveFunction)
                                and prim.prim is P.partial):
                            args = fn.inputs[2:] + args
                            fn = fn.inputs[1]
                            collapse = True
                            continue
            break
        if collapse:
            new_node = outref.node.graph.apply(fn, *args)
            return engine.ref(new_node, ctx)
        else:
            return None

    async def run(self, engine, outref, argrefs):
        """Run the inference."""
        argvals = tuple([await ref.get() for ref in argrefs])
        if argvals not in self.cache:
            args = tuple(VirtualReference(arg)
                         for arg in tuple(self.args) + argvals)
            self.cache[argvals] = await self.fn.run(engine, outref, args)
        return self.cache[argvals]


class VirtualInferrer(Inferrer):
    """Inferrer for a specific args/output pair.

    Attributes:
        args: The one set of legal abstract values.
        output: The abstract result.

    """

    def __init__(self, args, output):
        """Initialize a VirtualInferrer."""
        super().__init__()
        self.args = args
        self.output = output

    async def infer(self, engine, *args):
        """Check args against self.args and return self.output."""
        if len(args) != len(self.args):
            raise MyiaTypeError('Wrong number of arguments')
        for given, expected in zip(args, self.args):
            engine.abstract_merge(given, expected)
        return self.output


class JInferrer(Inferrer):
    """Inferrer for a function transformed through J."""

    def __init__(self, fn, orig_fn):
        """Initialize a JInferrer."""
        super().__init__()
        self.fn = fn
        self.orig_fn = orig_fn

    def _jinv(self, x):
        assert isinstance(x, AbstractJTagged)
        return x.element

    async def _jtag(self, x):
        if isinstance(x, AbstractFunction):
            v = await x.get()
            return AbstractFunction(*[JTransformedFunction(poss)
                                      for poss in v])
        return AbstractJTagged(x)

    async def run(self, engine, outref, argrefs):
        """Run the inference."""
        args = tuple([await ref.get() for ref in argrefs])
        if args not in self.cache:
            jinv_args = tuple(self._jinv(a) for a in args)
            jinv_argrefs = tuple(VirtualReference(arg)
                                 for arg in jinv_args)
            res = await self.fn.run(engine, None, jinv_argrefs)
            res_wrapped = await self._jtag(res)
            orig_fn = AbstractFunction(self.orig_fn)
            bparams = [sensitivity_transform(orig_fn)]
            bparams += [sensitivity_transform(a) for a in args]
            bparams_final = AbstractTuple(bparams)
            bprop = AbstractFunction(
                VirtualFunction(
                    (sensitivity_transform(res),),
                    bparams_final
                )
            )
            self.cache[args] = AbstractTuple([res_wrapped, bprop])
        return self.cache[args]


async def _inf_helper(engine, inf, outref, argrefs, p):
    result = await inf.run(engine, outref, argrefs)
    p.set_result(result)


async def execute_inferrers(engine, inferrers, outref, argrefs):
    """Execute a set of inferrers on a tuple of References.

    The results of the inferrers will be bound together and an error will
    be raised eventually if they cannot be merged.
    """
    reroutes = set([await inf.reroute(engine, outref, argrefs)
                    for inf in inferrers])
    if len(reroutes) > 1:
        # Unlikely to happen naturally, so I'm leaving it as an assert for the
        # time being.
        raise AssertionError('Only one macro may be used at a call point.')
    newref, = reroutes
    if newref is not None:
        return await engine.reroute(outref, newref)

    if len(inferrers) == 1:
        inf, = inferrers
        return await inf.run(engine, outref, argrefs)

    else:
        pending = []
        for inf in inferrers:
            p = engine.loop.create_pending(
                resolve=None,
                priority=lambda: None
            )
            pending.append(p)
            engine.loop.schedule(
                _inf_helper(engine, inf, outref, argrefs, p)
            )

        return bind(engine.loop, None, [], pending)
