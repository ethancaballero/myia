
import pytest
from pytest import mark
from copy import copy
from types import FunctionType

from myia.abstract import from_value, AbstractJTagged
from myia.pipeline import standard_resources, standard_pipeline, \
    PipelineDefinition, steps, pipeline_function
from myia.composite import grad
from myia.debug.finite_diff import clean_args
from myia.grad import J as realJ
from myia.pipeline.steps import Validator
from myia.prim import ops as P, Primitive
from myia.utils import Profile  # , no_prof
from myia.validate import whitelist, validate_abstract
from myia.abstract.data import SHAPE, TYPE, VALUE, ANYTHING, AbstractScalar

from ..common import f32, MA, to_abstract_test

torch = pytest.importorskip("torch")
nn = torch.nn
F = torch.nn.functional

from myia.frontends import activate_frontend  # noqa: E402
activate_frontend('pytorch')

from myia.frontends.pytorch_abstract_types import \
    AbstractPyTorchTensor  # noqa: E402

compile_pipeline = standard_pipeline


# Uncomment this line to print values at specific precision
torch.set_printoptions(precision=10)


def get_backend_options(args, backend):
    device_type = args.dev

    backend_options_dict = {
        'pytorch': {'device': device_type},
        'nnvm': {'target': device_type, 'device_id': 0},
        'relay': {'target': device_type, 'device_id': 0}
    }

    backend_options = backend_options_dict[backend]

    return backend_options


# TODO: add relay support
# TODO: maybe fixture for return_backend=True and return_backend=False
@pytest.fixture(params=[
    pytest.param('pytorch'),
    pytest.param('nnvm')
])
def _backend_fixture(request):
    return request.param


class Args():

    def __init__(self):
        # device used
        self.dev = 'cpu'
        # backend used
        self.backend = 'pytorch'
        # numerical precision
        self.dtype = 'float32'


args = Args()


def compare_fwd(*tests, optimize=True, python=True, profile=False):
    """Decorate a function to parse and run it against pure Python.

    Returns a unit test that will parse the function, and then for
    each `inputs` tuple in `tests` it will check that the pure Python,
    undecorated function returns that same output.

    This uses the full myia pipeline.

    Arguments:
        tests: One or more inputs tuple.

    """
    fwd_pipeline = compile_pipeline if optimize else \
        compile_pipeline.configure({'opt.phases.main': []})

    def decorate(fn):
        def test(args):
            nonlocal profile
            if not isinstance(args, tuple):
                args = (args,)

            _fwd_test(fn, args,
                      pipeline=fwd_pipeline,
                      optimize=optimize,
                      python=python,
                      profile=profile)

        m = mark.parametrize('args', list(tests))(test)
        m.__orig__ = fn
        return m
    return decorate


grad_whitelist = whitelist | {P.J, P.Jinv}


@validate_abstract.variant
def grad_validate_abstract(self, t: AbstractJTagged):
    pass


step_grad_validate = Validator.partial(
    whitelist=grad_whitelist,
    validate_abstract=grad_validate_abstract
)


@pipeline_function
def grad_wrap(self, graph):
    if isinstance(graph, Primitive):
        jg = realJ(graph, self.resources)
        g = grad.make_gf(jg, jg.parameters, wrt=range(len(jg.parameters)),
                         dbg=jg.debug, sens_param=True)
    else:
        g = grad.make_gf(graph, graph.parameters,
                         wrt=range(len(graph.parameters)),
                         dbg=graph.debug, sens_param=True,
                         apply_j=True)
    return g


grad_pipeline = PipelineDefinition(
    resources=standard_resources,
    steps=dict(
        parse=steps.step_parse,
        resolve=steps.step_resolve,
        infer=steps.step_infer,
        specialize=steps.step_specialize,
        opt=steps.step_debug_opt,
        validate=step_grad_validate,
        # compile=steps.step_compile,
        export=steps.step_debug_export,
    )
)

"""
backend = args.backend
backend_options = get_backend_options(args, backend)

standard_pipeline = \
standard_pipeline.configure({
            'compile.backend': backend,
            'compile.backend_options': backend_options,
        })

grad_pipeline = \
grad_pipeline.configure({
            'compile.backend': backend,
            'compile.backend_options': backend_options,
        })
#"""


# TODO: should this also return grads with respect to kwargs
def pt_fn_grads(fn, *args, **kwargs):
    output = fn(*args, **kwargs)
    return torch.autograd.grad(
        output, args, torch.ones(output.shape))


APT_loss = AbstractPyTorchTensor(
    AbstractScalar({TYPE: f32, VALUE: ANYTHING}), {SHAPE: (1,)})
APT_0d_loss = AbstractPyTorchTensor(
    AbstractScalar({TYPE: f32, VALUE: ANYTHING}), {SHAPE: ()})


def _fwd_test(fn, args, pipeline=standard_pipeline,
              optimize=True, python=True, profile=False):
    if python:
        ref_result = fn(*map(copy, args))
    argspec = tuple(from_value(arg, broaden=True) for arg in args)
    if profile is True:
        profile = Profile()
    # res = pipeline.run(input=fn, argspec=argspec, profile=profile)
    res = pipeline.run(input=fn, argspec=argspec)
    # profile.print()
    myia_fn = res['output']
    myia_result = myia_fn(*map(copy, args))
    if python:
        # print("ref_result", ref_result)
        # print("myia_result", myia_result)
        assert torch.allclose(ref_result, myia_result, equal_nan=True)
        assert ref_result.shape == myia_result.shape

    return tuple(myia_result.shape)


def _grad_test(fn, obj, args,
               sens_type,
               pipeline=grad_pipeline,
               rel_error=1e-3):

    pytorch_grads = pt_fn_grads(fn, *args)

    sens_type_shape = sens_type
    if sens_type == ():
        sens_type = APT_0d_loss
    elif sens_type == (1,):
        sens_type = APT_loss
    else:
        sens_type = AbstractPyTorchTensor(
            AbstractScalar({TYPE: f32, VALUE: ANYTHING}), {SHAPE: sens_type})

    pipeline = standard_pipeline
    pipeline = pipeline.insert_after('parse', grad_wrap=grad_wrap)
    argspec = tuple(from_value(arg, broaden=True) for arg in clean_args(args))
    sens_type = to_abstract_test(sens_type)
    if isinstance(obj, FunctionType):
        res = pipeline.run(input=obj, argspec=[*argspec, sens_type])
    else:
        pip = pipeline.configure(parse=False)
        res = pip.run(graph=obj, argspec=[*argspec, sens_type])

    if sens_type == APT_loss:
        sens = torch.Tensor([1.0])
    elif sens_type == APT_0d_loss:
        sens = torch.Tensor([1.0]).reshape(())
    else:
        sens = torch.ones(sens_type_shape)

    myia_grads = res['output'](*args, sens)

    for pt_g, my_g in zip(pytorch_grads, myia_grads):
        # print("pytorch_grad", pt_g)
        # print("myia_grad", my_g)
        assert torch.allclose(
            pt_g, my_g, rtol=1e-05, atol=1e-06, equal_nan=True)


def compare_bwd(*tests, sens_type=APT_loss, pipeline=grad_pipeline,
                rel_error=1e-3):
    """Decorate a function to parse and run it against pure Python.

    Returns a unit test that will parse the function, and then for
    each `inputs` tuple in `tests` it will check that the pure Python,
    undecorated function returns that same output.

    Arguments:
        tests: One or more inputs tuple.

    """

    def decorate(fn):
        def test(args):
            if not isinstance(args, tuple):
                args = (args,)

            _grad_test(fn, fn, args, pipeline=pipeline, rel_error=rel_error,
                       sens_type=sens_type)

        m = pytest.mark.parametrize('args', list(tests))(test)
        m.__orig__ = fn
        return m
    return decorate


def compare_fwd_and_bwd(*tests, optimize=True, python=True, profile=False,
                        sens_type=APT_0d_loss, pipeline=grad_pipeline,
                        rel_error=1e-3):
    """Decorate a function to parse and run it against pure Python.

    Returns a unit test that will parse the function, and then for
    each `inputs` tuple in `tests` it will check that the pure Python,
    undecorated function returns that same output.

    Arguments:
        tests: One or more inputs tuple.

    """

    fwd_pipeline = compile_pipeline if optimize else \
        compile_pipeline.configure({'opt.phases.main': []})

    def decorate(fn):
        def test(args):
            if not isinstance(args, tuple):
                args = (args,)
            out_shape = _fwd_test(fn, args, pipeline=fwd_pipeline,
                                  optimize=optimize, python=python,
                                  profile=profile)
            _grad_test(fn, fn, args, pipeline=pipeline,
                       rel_error=rel_error, sens_type=out_shape)

        m = pytest.mark.parametrize('args', list(tests))(test)
        m.__orig__ = fn
        return m
    return decorate


'''
def _name_args_helper(name, args):
    return [(name, args) for arg in args]
    #'''


# THIS TEST ALL OPS that are in dir of "torch" or "torch.tensor"
# all_torch_ops = dir(torch)
# all_torch_tensor_ops = dir(torch.Tensor([5.49670]))


all_torch_ops__1_tensor_arg = all_torch_tensor_ops__1_tensor_arg = \
    [
        'exp',
        'log',
        'relu',
        'sigmoid',
        'sum',
        'tanh',
    ]
"""
[
'relu',
]
"""


single_tensor_args = (
    (nn.Parameter(torch.Tensor([2.1]).reshape(()))),
    (nn.Parameter(torch.Tensor([2.1]))),
    (nn.Parameter(torch.Tensor([-2.2]))),
    (nn.Parameter(torch.Tensor(MA(2, 3)))),
)


@pytest.mark.parametrize(
    'name,args',
    [(op, single_tensor_args) for op in all_torch_ops__1_tensor_arg]
)
@pytest.mark.timeout(10)
def test_torch_ops__1_tensor_arg(name, args):
    def fn1(x):
        return getattr(torch, name)(x)

    if not isinstance(args, tuple):
        args = (args,)

    for arg in args:
        out_shape = _fwd_test(fn1, (arg,))
        _grad_test(fn1, fn1, (arg,), sens_type=out_shape)


@pytest.mark.parametrize(
    'name,args',
    [(op, single_tensor_args) for op in all_torch_tensor_ops__1_tensor_arg]
)
@pytest.mark.timeout(10)
def test_torch_tensor_ops__1_tensor_arg(name, args):
    def fn1(x):
        return getattr(x, name)()

    if not isinstance(args, tuple):
        args = (args,)

    for arg in args:
        out_shape = _fwd_test(fn1, (arg,))
        _grad_test(fn1, fn1, (arg,), sens_type=out_shape)


all_torch_ops__1_tensor_arg__fwd_only = \
    all_torch_tensor_ops__1_tensor_arg__fwd_only = \
    [
        'zeros_like',
    ]

'''
@pytest.mark.parametrize(
    'name,args',
    [(op, single_tensor_args) for op in all_torch_ops__1_tensor_arg__fwd_only]
    )
@pytest.mark.timeout(5)
def test_torch_ops__1_tensor_arg__fwd_only(name, args):
    def fn1(x):
        return getattr(torch, name)(x)


    if not isinstance(args, tuple):
        args = (args,)


    for arg in args:
        out_shape = _fwd_test(fn1, (arg,))


@pytest.mark.parametrize(
    'name,args',
    [(op, single_tensor_args) for op
     in all_torch_tensor_ops__1_tensor_arg__fwd_only]
    )
@pytest.mark.timeout(5)
def test_torch_tensor_ops__1_tensor_arg__fwd_only(name, args):
    def fn1(x):
        return getattr(x, name)()

    if not isinstance(args, tuple):
        args = (args,)

    for arg in args:
        out_shape = _fwd_test(fn1, (arg,))
#'''

all_torch_ops__2_args = all_torch_tensor_ops____2_args = \
    [
        'reshape',
        'sum',  # version with dim arg to reduce over
        't',
        'view',
    ]
