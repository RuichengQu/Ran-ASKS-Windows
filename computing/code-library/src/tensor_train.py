"""Small real-valued TT-matrix kernels, using C-order index conventions."""
from __future__ import annotations

import numpy as np


def validated_cores(cores):
    if not isinstance(cores, (list, tuple)) or not cores:
        raise ValueError("cores must be a nonempty list or tuple")
    arrays = []
    previous_rank = 1
    for core in cores:
        array = np.asarray(core)
        if array.ndim != 4 or any(size < 1 for size in array.shape):
            raise ValueError("each core must have shape (left_rank, output_mode, input_mode, right_rank)")
        if array.dtype.kind not in "fiu" or not np.isfinite(array).all():
            raise ValueError("cores must contain finite real numbers")
        if array.shape[0] != previous_rank:
            raise ValueError("adjacent TT ranks must agree and the first rank must be one")
        previous_rank = array.shape[3]
        arrays.append(array.astype(np.float64, copy=False))
    if previous_rank != 1:
        raise ValueError("the final TT rank must be one")
    return arrays


def tt_parameter_count(cores):
    """Count weight entries in the cores; bias parameters are not included."""
    return sum(core.size for core in validated_cores(cores))


def tt_to_dense(cores):
    """Materialize W for small reference checks, not production inference."""
    arrays = validated_cores(cores)
    tensor = arrays[0][0]
    for core in arrays[1:]:
        tensor = np.tensordot(tensor, core, axes=([-1], [0]))
    tensor = tensor[..., 0]
    output_axes = list(range(0, 2 * len(arrays), 2))
    input_axes = list(range(1, 2 * len(arrays), 2))
    output_size = int(np.prod([core.shape[1] for core in arrays]))
    input_size = int(np.prod([core.shape[2] for core in arrays]))
    result = tensor.transpose(output_axes + input_axes).reshape(output_size, input_size)
    if not np.isfinite(result).all():
        raise ValueError("TT materialization produced non-finite values")
    return result


def forward_states(arrays, vector):
    """Cache contraction states for the adjoint, using validated cores."""
    vector = np.asarray(vector)
    input_modes = [core.shape[2] for core in arrays]
    if vector.ndim != 1 or vector.size != int(np.prod(input_modes)):
        raise ValueError("vector shape must match the product of input modes")
    if vector.dtype.kind not in "fiu" or not np.isfinite(vector).all():
        raise ValueError("vector must contain finite real numbers")
    state = vector.astype(np.float64, copy=False).reshape((1, *input_modes))
    states = [state]
    for position, core in enumerate(arrays):
        contracted = np.tensordot(state, core, axes=([position, position + 1], [0, 2]))
        state = np.moveaxis(contracted, [-2, -1], [position, position + 1])
        if not np.isfinite(state).all():
            raise ValueError("TT contraction produced non-finite values")
        states.append(state)
    return states


def tt_matvec(cores, vector, bias=None):
    """Compute W @ x + b by contraction without materializing W."""
    arrays = validated_cores(cores)
    result = forward_states(arrays, vector)[-1][..., 0].reshape(-1)
    if bias is not None:
        bias = np.asarray(bias)
        if bias.shape != result.shape or bias.dtype.kind not in "fiu" or not np.isfinite(bias).all():
            raise ValueError("bias must be a finite real vector matching output modes")
        result = result + bias
    if not np.isfinite(result).all():
        raise ValueError("TT contraction produced non-finite values")
    return result


def tt_vjp(cores, vector, output_cotangent):
    """Return core, input and affine-bias adjoints for a supplied dL/dy."""
    arrays = validated_cores(cores)
    states = forward_states(arrays, vector)
    cotangent = np.asarray(output_cotangent)
    output_shape = [core.shape[1] for core in arrays]
    if cotangent.shape != (int(np.prod(output_shape)),):
        raise ValueError("output cotangent must match output modes")
    if cotangent.dtype.kind not in "fiu" or not np.isfinite(cotangent).all():
        raise ValueError("output cotangent must contain finite real numbers")
    adjoint = cotangent.astype(np.float64, copy=False).reshape((*output_shape, 1))
    core_gradients = [None] * len(arrays)
    for position in reversed(range(len(arrays))):
        state = states[position]
        raw_adjoint = np.moveaxis(adjoint, [position, position + 1], [-2, -1])
        retained_axes = [axis for axis in range(state.ndim) if axis not in {position, position + 1}]
        core_gradient = np.tensordot(state, raw_adjoint,
                                    axes=(retained_axes, list(range(len(retained_axes)))))
        core_gradients[position] = core_gradient.transpose(0, 2, 1, 3)
        state_gradient = np.tensordot(raw_adjoint, arrays[position], axes=([-2, -1], [1, 3]))
        adjoint = np.moveaxis(state_gradient, [-2, -1], [position, position + 1])
    gradients = {"cores": core_gradients, "input": adjoint[0].reshape(-1),
                 "bias": cotangent.astype(np.float64, copy=True)}
    if not all(np.isfinite(value).all() for value in [*core_gradients, gradients["input"], gradients["bias"]]):
        raise ValueError("TT adjoint produced non-finite values")
    return gradients
