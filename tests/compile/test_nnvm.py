# Most of the tests ar in test_backend, this is just for nnvm-specific
# tests that can't be made generic.

import numpy as np
import pytest

from myia import dtype

try:
    from myia.compile.backends import nnvm
except ImportError:
    pytestmark = pytest.mark.skip(f"Can't import nnvm")


@pytest.mark.gpu
def test_nnvm_backend_bad_device():
    with pytest.raises(RuntimeError):
        nnvm.NNVMBackend(target='cuda', device_id=31)


@pytest.mark.gpu
def test_nnvm_cross_context():
    backend_cuda = nnvm.NNVMBackend(target='cuda', device_id=0)
    backend_cpu = nnvm.NNVMBackend(target='cpu')

    v = np.ndarray([1, 2, 3])
    tp = dtype.Int[64]

    t_cuda = backend_cuda.from_numpy(v)
    t_cpu = backend_cpu.from_numpy(v)

    with pytest.raises(RuntimeError):
        backend_cuda.check_array(t_cpu, tp)

    with pytest.raises(RuntimeError):
        backend_cpu.check_array(t_cuda, tp)

    dlp = backend_cuda.to_dlpack(t_cuda)
    nt = backend_cpu.from_dlpack(dlp)
    nv = backend_cpu.to_numpy(nt)

    assert (v == nv).all()
