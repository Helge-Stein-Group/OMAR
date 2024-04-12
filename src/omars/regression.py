from copy import deepcopy
from dataclasses import dataclass, field

import numpy as np
from scipy.linalg import qr, solve_triangular, qr_insert


@dataclass
class Basis:
    v: list[int] = field(default_factory=list)  # Index of variable
    t: np.ndarray = field(
        default_factory=lambda: np.array([], dtype=float))  # Knots
    hinge: list[bool] = field(default_factory=list)  # Whether to apply the hinge

    def __call__(self, x: np.ndarray) -> np.ndarray:
        assert x.ndim == 2

        if len(self.v) == 0:
            return np.ones(x.shape[0])
        result = x[:, self.v] - self.t
        np.maximum(np.zeros((x.shape[0], len(self.v)), dtype=float), result,
                   where=self.hinge, out=result)
        return result.prod(axis=1)

    def add(self, v: int, t: float, hinge: bool):
        assert isinstance(v, int)
        assert isinstance(t, float)
        assert isinstance(hinge, bool)

        self.v.append(v)
        self.t = np.append(self.t, t)
        self.hinge.append(hinge)


@dataclass
class Model:
    basis: list[Basis] = field(default_factory=lambda: [Basis()])
    coefficients: np.ndarray = None
    fit_matrix: np.ndarray = None
    covariance_matrix: np.ndarray = None
    right_hand_side: np.ndarray = None
    gcv: float = None

    def data_matrix(self, x: np.ndarray) -> np.ndarray:
        assert x.ndim == 2

        return np.column_stack([basis(x) for basis in self.basis])

    def __call__(self, x: np.ndarray) -> np.ndarray:
        assert x.ndim == 2

        return self.data_matrix(x) @ self.coefficients

    def __len__(self):
        return len(self.basis)

    def __getitem__(self, i: int):
        assert isinstance(i, int)
        assert i < len(self.basis)

        return Model([self.basis[i]], self.coefficients[i].reshape(1, -1))

    def add(self, bases: list[Basis]):
        assert all(isinstance(b, Basis) for b in bases)

        self.basis.extend(bases)
        return self

    def remove(self, i: int):
        assert isinstance(i, int)
        assert i < len(self.basis)

        del self.basis[i]
        return self

    def fit(self, x: np.ndarray, y: np.ndarray, d: float = 3) -> tuple[
        np.ndarray, np.ndarray]:
        assert x.ndim == 2
        assert y.ndim == 1
        assert x.shape[0] == y.shape[0]
        assert isinstance(d, (int, float))

        '''
        ATTENTION !!!
        The basis through the forward pass does not need to be linearly independent, 
        which may lead to a singular matrix. Here we can either add a small diagonal 
        value or set the dependent coefficients to 0 to obtain a unique solution again.
        '''
        self.fit_matrix = self.data_matrix(x)

        self.covariance_matrix = self.fit_matrix.T @ self.fit_matrix
        self.covariance_matrix -= (np.sum(self.fit_matrix.T, axis=1, keepdims=True) *
                                   np.mean(self.fit_matrix, axis=0, keepdims=True))
        self.covariance_matrix += np.eye(self.covariance_matrix.shape[0]) * 1e-6

        self.right_hand_side = self.fit_matrix.T @ y
        self.right_hand_side -= np.mean(y) * np.sum(self.fit_matrix, axis=0, keepdims=True)

        q, r = qr(self.covariance_matrix)

        self.coefficients = solve_triangular(r, q.T @ self.right_hand_side)

        y_pred = self.fit_matrix @ self.coefficients
        mse = np.sum((y - y_pred) ** 2)

        c_m = len(self.basis) + d * (len(self.basis) - 1)
        self.gcv = mse / len(y) / (1 - c_m / len(y)) ** 2

        return q, r

    def update_fit(self, x: np.ndarray, y: np.ndarray, q: np.ndarray,
                   r: np.ndarray, d: float = 3) -> None:
        assert x.ndim == 2
        assert y.ndim == 1
        assert x.shape[0] == y.shape[0]
        assert q.shape[0] == q.shape[1] == len(self.basis) - 1
        assert r.shape[0] == r.shape[1] == len(self.basis) - 1
        assert isinstance(r, np.ndarray)
        assert isinstance(d, (int, float))

        # correction for means, removing them here
        self.covariance_matrix += (np.sum(self.fit_matrix.T, axis=1, keepdims=True) *
                                   np.mean(self.fit_matrix, axis=0, keepdims=True))

        # Update stuff
        self.fit_matrix = np.column_stack([self.fit_matrix, self.basis[-1](x)])

        covariance_addition = self.fit_matrix.T @ self.fit_matrix[:, -1]
        covariance_addition[-1] += 1e-6
        self.covariance_matrix = np.row_stack([self.covariance_matrix, covariance_addition[:-1]])
        self.covariance_matrix = np.column_stack([self.covariance_matrix, covariance_addition])
        self.covariance_matrix -= (np.sum(self.fit_matrix.T, axis=1, keepdims=True) *
                                   np.mean(self.fit_matrix, axis=0, keepdims=True))

        right_hand_side_addition = self.fit_matrix[:, -1].T @ y
        right_hand_side_addition -= np.mean(y) * np.sum(self.fit_matrix[:, -1], axis=0, keepdims=True)
        self.right_hand_side = np.append(self.right_hand_side, right_hand_side_addition)

        # Update QR decomposition
        q, r = qr_insert(q, r, covariance_addition[:-1], q.shape[1], "col")
        q, r = qr_insert(q, r, covariance_addition, q.shape[0], "row")

        self.coefficients = solve_triangular(r, q.T @ self.right_hand_side)

        y_pred = self.fit_matrix @ self.coefficients
        mse = np.sum((y - y_pred) ** 2)

        c_m = len(self.basis) + d * (len(self.basis) - 1)
        self.gcv = mse / len(y) / (1 - c_m / len(y)) ** 2

        return q, r


def forward_pass(x: np.ndarray, y: np.ndarray, m_max: int) -> Model:
    assert x.ndim == 2
    assert y.ndim == 1
    assert x.shape[0] == y.shape[0]
    assert isinstance(m_max, int)

    covariates = set(range(x.shape[1]))
    model = Model()  # Initial fit computation
    while len(model) < m_max:
        best_gcv = np.inf
        best_candidate_model = None
        for m, selected_basis in enumerate(model.basis):
            ineligible_covariates = set(selected_basis.v)
            eligible_covariates = covariates - ineligible_covariates
            for v in eligible_covariates:
                eligible_knots = x[:, v][np.where(selected_basis(x) > 0)]
                eligible_knots[::-1].sort()
                candidate_model = deepcopy(model)
                unhinged_candidate = deepcopy(selected_basis)
                unhinged_candidate.add(v, 0.0, False)
                candidate_model.add([unhinged_candidate])  # Insert fit computation
                hinged_candidate = deepcopy(selected_basis)
                hinged_candidate.add(v, t, True)
                candidate_model.add([hinged_candidate])  # Insert fit computation
                candidate_model.fit(x, y)
                for t in eligible_knots:
                    hinged_candidate = deepcopy(selected_basis)
                    hinged_candidate.add(v, t, True)
                    candidate_model.basis[-1] = hinged_candidate
                    candidate_model.fit(x, y)  # Update fit computation
                    if candidate_model.gcv < best_gcv:
                        best_gcv = candidate_model.gcv
                        best_candidate_model = deepcopy(candidate_model)

        model = best_candidate_model

    return model


def backward_pass(x: np.ndarray, y: np.ndarray, model: Model) -> Model:
    assert x.ndim == 2
    assert y.ndim == 1
    assert x.shape[0] == y.shape[0]
    assert isinstance(model, Model)

    best_model = model
    best_trimmed_model = deepcopy(model)
    best_gcv = model.gcv

    while len(best_trimmed_model) > 1:
        best_trimmed_gcv = np.inf
        previous_model = deepcopy(best_trimmed_model)
        for i in range(len(previous_model)):
            if i == 0:  # First basis function (constant 1) cannot be excluded
                continue
            trimmed_model = deepcopy(previous_model).remove(i)
            trimmed_model.fit(x, y)
            if trimmed_model.gcv < best_trimmed_gcv:
                best_trimmed_gcv = trimmed_model.gcv
                best_trimmed_model = trimmed_model
            if trimmed_model.gcv < best_gcv:
                best_gcv = trimmed_model.gcv
                best_model = trimmed_model

    return best_model


def fit(x: np.ndarray, y: np.ndarray, m_max: int) -> Model:
    model = forward_pass(x, y, m_max)
    model = backward_pass(x, y, model)

    return model
