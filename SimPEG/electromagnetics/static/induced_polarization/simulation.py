import numpy as np
import dask
import dask.array as da
import multiprocessing
import scipy.sparse as sp
import sys

from .... import props
from ....data import Data
from ....utils import mkvc, sdiag, Zero
from ...base import BaseEMSimulation

from ..resistivity.fields import FieldsDC, Fields_CC, Fields_N
from ..resistivity import Problem3D_CC as BaseProblem3D_CC
from ..resistivity import Problem3D_N as BaseProblem3D_N
# from .survey import Survey


class BaseIPSimulation(BaseEMSimulation):

    sigma = props.PhysicalProperty(
        "Electrical conductivity (S/m)"
    )

    rho = props.PhysicalProperty(
        "Electrical resistivity (Ohm m)"
    )

    props.Reciprocal(sigma, rho)

    eta, etaMap, etaDeriv = props.Invertible(
        "Electrical Chargeability"
    )

    # surveyPair = Survey
    fieldsPair = FieldsDC
    Ainv = None
    _f = None
    storeJ = False
    _Jmatrix = None
    sign = None
    data_type = 'volt'
    _pred = None

    def fields(self, m):
        if self.verbose is True:
            print(">> Compute fields")

        if self._f is None:
            self._f = self.fieldsPair(self)
            if self.Ainv is None:
                A = self.getA()
                self.Ainv = self.Solver(A, **self.solver_opts)
            RHS = self.getRHS()
            u = self.Ainv * RHS
            Srcs = self.survey.srcList
            self._f[Srcs, self._solutionType] = u

            # Compute DC voltage
            if self.data_type == 'apparent_chargeability':
                if self.verbose is True:
                    print(">> Data type is apparaent chargeability")
                for src in self.survey.srcList:
                    for rx in src.rxList:
                        rx._dc_voltage = rx.eval(src, self.mesh, self._f)
                        rx.data_type = self.data_type
                        rx._Ps = {}

        self._pred = self.forward(m, f=self._f)

        return self._f

    def dpred(self, m=None, f=None):
        """
            Predicted data.
            .. math::
                d_\\text{pred} = Pf(m)
        """
        if f is None:
            f = self.fields(m)

        return self._pred

    def getJ(self, m, f=None):
        """
            Generate Full sensitivity matrix
        """
        self.model = m

        if self.verbose:
            print("Calculating J and storing")

        if self._Jmatrix is not None:
            return self._Jmatrix
        else:

            if f is None:
                f = self.fields(m)
            self._Jmatrix = (self._Jtvec(m, v=None, f=f)).T

            # delete fields after computing sensitivity
            # del f
            self._f = []
            # clean all factorization
            if self.Ainv is not None:
                self.Ainv.clean()

        return self._Jmatrix

    # @profile
    def Jvec(self, m, v, f=None):

        self.model = m

        # When sensitivity matrix J is stored
        if self.storeJ:
            J = self.getJ(m, f=f)
            Jv = mkvc(np.dot(J, v))
            return self.sign * Jv

        else:

            if f is None:
                f = self.fields(m)

            Jv = []

            for src in self.survey.srcList:
                # solution vector
                u_src = f[src, self._solutionType]
                dA_dm_v = self.getADeriv(u_src.flatten(), v, adjoint=False)
                dA_dm_v = da.from_delayed(dA_dm_v, shape=self.model.shape, dtype=float)
                dRHS_dm_v = self.getRHSDeriv(src, v)
                dRHS_dm_v = da.from_delayed(dRHS_dm_v, shape=self.model.shape, dtype=float)
                du_dm_v = self.Ainv * (- dA_dm_v + dRHS_dm_v).compute()

                for rx in src.rxList:
                    df_dmFun = getattr(f, '_{0!s}Deriv'.format(rx.projField), None)
                    df_dm_v = df_dmFun(src, du_dm_v, v, adjoint=False)
                    Jv.append(rx.evalDeriv(src, self.mesh, f, df_dm_v))

            # Conductivity (d u / d log sigma) - EB form
            # Resistivity (d u / d log rho) - HJ form
            return self.sign*np.hstack(Jv)

    def forward(self, m, f=None):
        return self.Jvec(m, m, f=f)

    def Jtvec(self, m, v, f=None):
        """
            Compute adjoint sensitivity matrix (J^T) and vector (v) product.

        """

        # When sensitivity matrix J is stored
        if self.storeJ:
            J = self.getJ(m, f=f)
            Jtv = mkvc(np.dot(J.T, v))
            return self.sign * Jtv

        else:
            self.model = m

            if f is None:
                f = self.fields(m)
            return self._Jtvec(m, v=v, f=f)

    def _Jtvec(self, m, v=None, f=None):
        """
            Compute adjoint sensitivity matrix (J^T) and vector (v) product.
            Full J matrix can be computed by inputing v=None
        """

        if v is not None:
            # Ensure v is a data object.
            if not isinstance(v, Data):
                v = Data(self.survey, v)
            Jtv = np.zeros(m.size)
        else:
            # This is for forming full sensitivity matrix
            Jtv = np.zeros((self.model.size, self.survey.nD), order='F')
            istrt = int(0)
            iend = int(0)

        for isrc, src in enumerate(self.survey.srcList):
            u_src = f[src, self._solutionType]
            if self.storeJ:
                # TODO: use logging package
                sys.stdout.write(("\r %d / %d") % (isrc+1, self.survey.nSrc))
                sys.stdout.flush()

            for rx in src.rxList:
                if v is not None:
                    PTv = rx.evalDeriv(
                        src, self.mesh, f, v[src, rx], adjoint=True
                    )  # wrt f, need possibility wrt m
                    df_duTFun = getattr(
                        f, '_{0!s}Deriv'.format(rx.projField), None
                    )
                    df_duT, df_dmT = df_duTFun(src, None, PTv, adjoint=True)
                    ATinvdf_duT = self.Ainv * df_duT
                    dA_dmT = self.getADeriv(
                        u_src.flatten(), ATinvdf_duT, adjoint=True
                    )
                    dA_dmT = da.from_delayed(dA_dmT, shape=self.model.shape, dtype=float)
                    dRHS_dmT = self.getRHSDeriv(src, ATinvdf_duT, adjoint=True)
                    dRHS_dmT = da.from_delayed(dRHS_dmT, shape=self.model.shape, dtype=float)
                    du_dmT = (-dA_dmT + dRHS_dmT).compute()
                    Jtv += (df_dmT + du_dmT).astype(float)
                else:
                    P = rx.getP(self.mesh, rx.projGLoc(f)).toarray()
                    ATinvdf_duT = self.Ainv * (P.T)
                    dA_dmT = self.getADeriv(
                        u_src, ATinvdf_duT, adjoint=True
                    )

                    iend = istrt + rx.nD
                    if rx.nD == 1:
                        Jtv[:, istrt] = -dA_dmT
                    else:
                        Jtv[:, istrt:iend] = -dA_dmT
                    istrt += rx.nD

        # Conductivity ((d u / d log sigma).T) - EB form
        # Resistivity ((d u / d log rho).T) - HJ form

        if v is not None:
            return self.sign*mkvc(Jtv)
        else:
            return Jtv
        return

    def getSourceTerm(self):
        """
        takes concept of source and turns it into a matrix
        """
        """
        Evaluates the sources, and puts them in matrix form

        :rtype: (numpy.ndarray, numpy.ndarray)
        :return: q (nC or nN, nSrc)
        """

        Srcs = self.survey.srcList

        if self._formulation == 'EB':
            n = self.mesh.nN
            # return NotImplementedError

        elif self._formulation == 'HJ':
            n = self.mesh.nC

        q = np.zeros((n, len(Srcs)))

        for i, src in enumerate(Srcs):
            q[:, i] = src.eval(self)
        return q

    def delete_these_for_sensitivity(self):
        del self._Jmatrix, self._MfRhoI, self._MeSigma

    @property
    def deleteTheseOnModelUpdate(self):
        toDelete = []
        return toDelete

    @property
    def MfRhoDerivMat(self):
        """
        Derivative of MfRho with respect to the model
        """
        if getattr(self, '_MfRhoDerivMat', None) is None:
            drho_dlogrho = sdiag(self.rho)*self.etaDeriv
            self._MfRhoDerivMat = self.mesh.getFaceInnerProductDeriv(
                np.ones(self.mesh.nC)
            )(np.ones(self.mesh.nF)) * drho_dlogrho
        return self._MfRhoDerivMat

    def MfRhoIDeriv(self, u, v, adjoint=False):
        """
            Derivative of :code:`MfRhoI` with respect to the model.
        """
        dMfRhoI_dI = -self.MfRhoI**2
        if self.storeInnerProduct:
            if adjoint:
                return self.MfRhoDerivMat.T * (
                    sdiag(u) * (dMfRhoI_dI.T * v)
                )
            else:
                return dMfRhoI_dI * (sdiag(u) * (self.MfRhoDerivMat*v))
        else:
            dMf_drho = self.mesh.getFaceInnerProductDeriv(self.rho)(u)
            drho_dlogrho = sdiag(self.rho)*self.etaDeriv
            if adjoint:
                return drho_dlogrho.T * (dMf_drho.T * (dMfRhoI_dI.T*v))
            else:
                return dMfRhoI_dI * (dMf_drho * (drho_dlogrho*v))

    @property
    def MeSigmaDerivMat(self):
        """
        Derivative of MeSigma with respect to the model
        """

        if getattr(self, '_MeSigmaDerivMat', None) is None:
            dsigma_dlogsigma = sdiag(self.sigma)*self.etaDeriv
            self._MeSigmaDerivMat = self.mesh.getEdgeInnerProductDeriv(
                np.ones(self.mesh.nC)
            )(np.ones(self.mesh.nE)) * dsigma_dlogsigma
        return self._MeSigmaDerivMat

    # TODO: This should take a vector
    def MeSigmaDeriv(self, u, v, adjoint=False):
        """
        Derivative of MeSigma with respect to the model times a vector (u)
        """
        if self.storeInnerProduct:
            if adjoint:
                return self.MeSigmaDerivMat.T * (sdiag(u)*v)
            else:
                return sdiag(u)*(self.MeSigmaDerivMat * v)
        else:
            dsigma_dlogsigma = sdiag(self.sigma)*self.etaDeriv
            if adjoint:
                return (
                    dsigma_dlogsigma.T * (
                        self.mesh.getEdgeInnerProductDeriv(self.sigma)(u).T * v
                    )
                )
            else:
                return (
                    self.mesh.getEdgeInnerProductDeriv(self.sigma)(u) *
                    (dsigma_dlogsigma * v)
                )


class Problem3D_CC(BaseIPSimulation, BaseProblem3D_CC):

    _solutionType = 'phiSolution'
    _formulation = 'HJ'  # CC potentials means J is on faces
    fieldsPair = Fields_CC
    sign = 1.
    bc_type = 'Dirichlet'

    def __init__(self, mesh, **kwargs):
        super(Problem3D_CC, self).__init__(mesh, **kwargs)
        self.setBC()


class Problem3D_N(BaseIPSimulation, BaseProblem3D_N):

    _solutionType = 'phiSolution'
    _formulation = 'EB'  # N potentials means B is on faces
    fieldsPair = Fields_N
    sign = -1.

    def __init__(self, mesh, **kwargs):
        super(Problem3D_N, self).__init__(mesh, **kwargs)



