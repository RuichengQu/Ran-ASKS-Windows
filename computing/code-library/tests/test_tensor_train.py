"""Independent small-matrix checks, not paper training or performance claims."""
import itertools
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import tensor_train


def reference_matrix(cores):
    output_shape = [core.shape[1] for core in cores]
    input_shape = [core.shape[2] for core in cores]
    matrix = np.empty((int(np.prod(output_shape)), int(np.prod(input_shape))))
    for output_indices in itertools.product(*(range(size) for size in output_shape)):
        for input_indices in itertools.product(*(range(size) for size in input_shape)):
            product = np.ones((1, 1))
            for core, output_index, input_index in zip(cores, output_indices, input_indices):
                product = product @ core[:, output_index, input_index, :]
            matrix[np.ravel_multi_index(output_indices, output_shape),
                   np.ravel_multi_index(input_indices, input_shape)] = product[0, 0]
    return matrix


class TensorTrainTests(unittest.TestCase):
    def setUp(self):
        self.random = np.random.default_rng(41)
        self.cores = [self.random.normal(size=(1, 2, 3, 2)),
                      self.random.normal(size=(2, 3, 2, 2)),
                      self.random.normal(size=(2, 2, 2, 1))]

    def test_materialization_matches_elementwise_definition(self):
        np.testing.assert_allclose(tensor_train.tt_to_dense(self.cores), reference_matrix(self.cores),
                                   rtol=1e-12, atol=1e-12)

    def test_forward_matches_independent_dense_reference(self):
        vector = self.random.normal(size=12)
        bias = self.random.normal(size=12)
        expected = reference_matrix(self.cores) @ vector + bias
        np.testing.assert_allclose(tensor_train.tt_matvec(self.cores, vector, bias), expected,
                                   rtol=1e-12, atol=1e-12)

    def test_forward_does_not_call_materialization(self):
        with patch.object(tensor_train, "tt_to_dense", side_effect=AssertionError("dense path")):
            tensor_train.tt_matvec(self.cores, np.ones(12))

    def test_single_core_rectangular_matrix(self):
        matrix = np.arange(6.0).reshape(2, 3)
        cores = [matrix.reshape(1, 2, 3, 1)]
        np.testing.assert_array_equal(tensor_train.tt_to_dense(cores), matrix)
        np.testing.assert_array_equal(tensor_train.tt_matvec(cores, np.ones(3)), matrix @ np.ones(3))

    def test_rank_one_and_unequal_modes(self):
        cores = [self.random.normal(size=(1, 2, 3, 1)), self.random.normal(size=(1, 3, 4, 1))]
        np.testing.assert_allclose(tensor_train.tt_matvec(cores, np.ones(12)),
                                   reference_matrix(cores) @ np.ones(12), atol=1e-12)

    def test_count_excludes_bias(self):
        self.assertEqual(tensor_train.tt_parameter_count(self.cores), 12 + 24 + 8)

    def test_input_arrays_are_not_modified(self):
        originals = [core.copy() for core in self.cores]
        tensor_train.tt_matvec(self.cores, np.ones(12))
        for original, core in zip(originals, self.cores):
            np.testing.assert_array_equal(original, core)

    def test_invalid_cores_rejected(self):
        cases = [[], [np.ones((2, 2))], [np.ones((2, 2, 2, 1))],
                 [np.ones((1, 2, 2, 2))], [np.ones((1, 0, 2, 1))],
                 [np.full((1, 2, 2, 1), np.nan)], [np.ones((1, 2, 2, 1), dtype=complex)],
                 [np.ones((1, 2, 2, 2)), np.ones((3, 2, 2, 1))]]
        for cores in cases:
            with self.subTest(shapes=[core.shape for core in cores]), self.assertRaises(ValueError):
                tensor_train.tt_to_dense(cores)

    def test_invalid_vector_and_bias_rejected(self):
        for vector in [np.ones(11), np.ones((1, 12)), np.full(12, np.nan), np.ones(12, dtype=complex)]:
            with self.subTest(shape=vector.shape), self.assertRaises(ValueError):
                tensor_train.tt_matvec(self.cores, vector)
        for bias in [np.ones(11), np.ones((1, 12)), np.full(12, np.inf)]:
            with self.subTest(shape=bias.shape), self.assertRaises(ValueError):
                tensor_train.tt_matvec(self.cores, np.ones(12), bias)

    def test_vjp_core_gradients_against_independent_finite_differences(self):
        cases = [self.cores, [self.random.normal(size=(1, 2, 3, 1))],
                 [self.random.normal(size=(1, 2, 3, 1)), self.random.normal(size=(1, 3, 4, 1))],
                 [self.random.normal(size=(1, 2, 2, 2)), self.random.normal(size=(2, 3, 2, 3)),
                  self.random.normal(size=(3, 2, 2, 1))]]
        for cores in cases:
            matrix = reference_matrix(cores)
            vector = self.random.normal(size=matrix.shape[1])
            cotangent = self.random.normal(size=matrix.shape[0])
            gradients = tensor_train.tt_vjp(cores, vector, cotangent)
            for core, gradient in zip(cores, gradients["cores"]):
                reference = np.empty_like(core)
                for index in np.ndindex(core.shape):
                    original = core[index]
                    core[index] = original + 1e-6
                    upper = cotangent @ (reference_matrix(cores) @ vector)
                    core[index] = original - 1e-6
                    lower = cotangent @ (reference_matrix(cores) @ vector)
                    core[index] = original
                    reference[index] = (upper - lower) / 2e-6
                np.testing.assert_allclose(gradient, reference, rtol=1e-5, atol=1e-7)
            np.testing.assert_allclose(gradients["input"], matrix.T @ cotangent, rtol=1e-12, atol=1e-12)
            np.testing.assert_array_equal(gradients["bias"], cotangent)

    def test_vjp_never_materializes_weight_matrix(self):
        with patch.object(tensor_train, "tt_to_dense", side_effect=AssertionError("dense path")):
            tensor_train.tt_vjp(self.cores, np.ones(12), np.ones(12))

    def test_vjp_does_not_mutate_inputs(self):
        originals = [core.copy() for core in self.cores]
        vector, cotangent = np.ones(12), np.ones(12)
        tensor_train.tt_vjp(self.cores, vector, cotangent)
        for original, core in zip(originals, self.cores):
            np.testing.assert_array_equal(original, core)
        np.testing.assert_array_equal(vector, np.ones(12))
        np.testing.assert_array_equal(cotangent, np.ones(12))

    def test_invalid_cotangents_rejected(self):
        for cotangent in [np.ones(11), np.ones((1, 12)), np.full(12, np.nan), np.ones(12, dtype=complex)]:
            with self.subTest(shape=cotangent.shape), self.assertRaises(ValueError):
                tensor_train.tt_vjp(self.cores, np.ones(12), cotangent)


if __name__ == "__main__":
    unittest.main()
