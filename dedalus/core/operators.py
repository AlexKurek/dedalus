"""
Abstract and built-in classes defining deferred operations on fields.

"""

from collections import defaultdict
from functools import partial, reduce
import numpy as np
from scipy import sparse
from numbers import Number

from .domain import Subdomain
from .field import Operand, Data, Array, Field
from .future import Future, FutureArray, FutureField
from ..tools.array import reshape_vector, apply_matrix, add_sparse, axslice
from ..tools.cache import CachedAttribute
from ..tools.dispatch import MultiClass
from ..tools.exceptions import NonlinearOperatorError
from ..tools.exceptions import SymbolicParsingError
from ..tools.exceptions import UndefinedParityError
from ..tools.general import unify, unify_attributes


class Rand:
    pass
Scalar = FutureScalar = Rand


# Use simple decorator to track parseable operators
parseables = {}
prefixes = {}

def parseable(*names):
    def register_op(op):
        for name in names:
            parseables[name] = op
        return op
    return register_op

def prefix(*names):
    def register_op(op):
        for name in names:
            prefixes[name] = op
        return op
    return register_op


# Other helpers
def is_integer(x):
    if isinstance(x, int):
        return True
    else:
        return x.is_integer()


class FieldCopy(FutureField, metaclass=MultiClass):
    """Operator making a new field copy of data."""

    name = 'FieldCopy'

    @classmethod
    def _preprocess_args(cls, arg, domain, **kw):
        arg = Operand.cast(arg, domain=domain)
        return (arg,), kw

    @classmethod
    def _check_args(cls, *args, **kw):
        match = (isinstance(args[i], types) for i,types in cls.argtypes.items())
        return all(match)

    def __init__(self, arg, **kw):
        super().__init__(arg, **kw)
        self.kw = {'domain': arg.domain}

    def _build_bases(self, arg0):
        return arg0.bases

    def __str__(self):
        return str(self.args[0])

    def check_conditions(self):
        return True

    def base(self):
        return FieldCopy

    def sym_diff(self, var):
        """Symbolically differentiate with respect to var."""
        return self.args[0].sym_diff(var)


class FieldCopyScalar(FieldCopy):

    argtypes = {0: (Scalar, FutureScalar)}

    def operate(self, out):
        if self.args[0].value == 0:
            # Copy in coeff layout
            out.set_layout(self._coeff_layout)
            out.data.fill(0)
        else:
            # Copy in grid layout
            out.set_layout(self._grid_layout)
            np.copyto(out.data, self.args[0].value)

    def __eq__(self, other):
        return self.args[0].__eq__(other)


class FieldCopyArray(FieldCopy):

    argtypes = {0: (Array, FutureArray)}

    def operate(self, out):
        # Copy in grid layout
        out.set_layout(self._grid_layout)
        np.copyto(out.data, self.args[0].data)


class FieldCopyField(FieldCopy):

    argtypes = {0: (Field, FutureField)}

    def operate(self, out):
        arg0, = self.args
        # Copy in current layout
        out.set_layout(arg0.layout)
        np.copyto(out.data, arg0.data)


class Operator(Future):

    @property
    def base(self):
        return type(self)

    def order(self, *ops):
        order = max(arg.order(*ops) for arg in self.args)
        if type(self) in ops:
            order += 1
        return order


class NonlinearOperator(Operator):

    def expand(self, *vars):
        """Return self."""
        return self

    def canonical_linear_form(self, *vars):
        """Raise if arguments contain specified variables (default: None)"""
        if self.has(*vars):
            raise NonlinearOperatorError("{} is a non-linear function of the specified variables.".format(str(self)))
        else:
            return self

    def split(self, *vars):
        if self.has(*vars):
            return [self, 0]
        else:
            return [0, self]


class GeneralFunction(NonlinearOperator, FutureField):
    """
    Operator wrapping a general python function.

    Parameters
    ----------
    domain : domain object
        Domain
    layout : layout object or identifier
        Layout of function output
    func : function
        Function producing field data
    args : list
        Arguments to pass to func
    kw : dict
        Keywords to pass to func
    out : field, optional
        Output field (default: new field)

    Notes
    -----
    On evaluation, this wrapper evaluates the provided funciton with the given
    arguments and keywords, and takes the output to be data in the specified
    layout, i.e.

        out[layout] = func(*args, **kw)

    """

    def __init__(self, domain, layout, func, args=[], kw={}, out=None,):

        # Required attributes
        self.args = list(args)
        self.original_args = list(args)
        self.domain = domain
        self.out = out
        self.last_id = None
        # Additional attributes
        self.layout = domain.distributor.get_layout_object(layout)
        self.func = func
        self.kw = kw
        self._field_arg_indices = [i for (i,arg) in enumerate(self.args) if is_fieldlike(arg)]
        try:
            self.name = func.__name__
        except AttributeError:
            self.name = str(func)
        self.build_metadata()

    def build_metadata(self):
        self.constant = np.array([False] * self.domain.dim)

    def check_conditions(self):
        # Fields must be in proper layout
        for i in self._field_arg_indices:
            if self.args[i].layout is not self.layout:
                return False
        return True

    def operate(self, out):
        # Apply func in proper layout
        for i in self._field_arg_indices:
            self.args[i].require_layout(self.layout)
        out.set_layout(self.layout)
        np.copyto(out.data, self.func(*self.args, **self.kw))


class UnaryGridFunction(NonlinearOperator, Future, metaclass=MultiClass):

    arity = 1
    supported = {ufunc.__name__: ufunc for ufunc in
        (np.absolute, np.sign, np.conj, np.exp, np.exp2, np.log, np.log2,
         np.log10, np.sqrt, np.square, np.sin, np.cos, np.tan, np.arcsin,
         np.arccos, np.arctan, np.sinh, np.cosh, np.tanh, np.arcsinh,
         np.arccosh, np.arctanh)}
    aliased = {'abs':np.absolute, 'conj':np.conjugate}
    # Add ufuncs and shortcuts to parseables
    parseables.update(supported)
    parseables.update(aliased)

    @classmethod
    def _preprocess_args(self, func, arg, **kw):
        #arg = Operand.cast(arg)
        return (func, arg), kw

    @classmethod
    def _check_args(cls, *args, **kw):
        match = (isinstance(args[i], types) for i,types in cls.argtypes.items())
        return all(match)

    def __init__(self, func, arg, **kw):
        #arg = Operand.cast(arg)
        super().__init__(arg, **kw)
        self.func = func
        self.name = func.__name__

    def _build_bases(self, arg0):
        bases = arg0.bases
        if all(basis is None for basis in bases):
            bases = arg0.domain
        return bases

    def meta_constant(self, axis):
        # Preserves constancy
        return self.args[0].meta[axis]['constant']

    def meta_parity(self, axis):
        # Preserving constancy -> even parity
        if self.args[0].meta[axis]['constant']:
            return 1
        elif self.args[0].meta[axis]['parity'] == 1:
            return 1
        else:
            raise UndefinedParityError("Unknown action of {} on odd parity.".format(self.name))

    def sym_diff(self, var):
        """Symbolically differentiate with respect to var."""
        diffmap = {np.absolute: lambda x: np.sign(x),
                   np.sign: lambda x: 0,
                   np.exp: lambda x: np.exp(x),
                   np.exp2: lambda x: np.exp2(x) * np.log(2),
                   np.log: lambda x: x**(-1),
                   np.log2: lambda x: (x * np.log(2))**(-1),
                   np.log10: lambda x: (x * np.log(10))**(-1),
                   np.sqrt: lambda x: (1/2) * x**(-1/2),
                   np.square: lambda x: 2*x,
                   np.sin: lambda x: np.cos(x),
                   np.cos: lambda x: -np.sin(x),
                   np.tan: lambda x: np.cos(x)**(-2),
                   np.arcsin: lambda x: (1 - x**2)**(-1/2),
                   np.arccos: lambda x: -(1 - x**2)**(-1/2),
                   np.arctan: lambda x: (1 + x**2)**(-1),
                   np.sinh: lambda x: np.cosh(x),
                   np.cosh: lambda x: np.sinh(x),
                   np.tanh: lambda x: np.cosh(x)**(-2),
                   np.arcsinh: lambda x: (x**2 + 1)**(-1/2),
                   np.arccosh: lambda x: (x**2 - 1)**(-1/2),
                   np.arctanh: lambda x: (1 - x**2)**(-1)}
        arg0 = self.args[0]
        diff0 = arg0.sym_diff(var)
        return diffmap[self.func](arg0) * diff0


class UnaryGridFunctionScalar(UnaryGridFunction, FutureScalar):

    argtypes = {1: (Scalar, FutureScalar)}

    def check_conditions(self):
        return True

    def operate(self, out):
        out.value = self.func(self.args[0].value)


class UnaryGridFunctionArray(UnaryGridFunction, FutureArray):

    argtypes = {1: (Array, FutureArray)}

    def check_conditions(self):
        return True

    def operate(self, out):
        self.func(self.args[0].data, out=out.data)


class UnaryGridFunctionField(UnaryGridFunction, FutureField):

    argtypes = {1: (Field, FutureField)}

    def check_conditions(self):
        # Field must be in grid layout
        return (self.args[0].layout is self._grid_layout)

    def operate(self, out):
        # References
        arg0, = self.args
        # Evaluate in grid layout
        arg0.require_grid_space()
        out.set_layout(self._grid_layout)
        self.func(arg0.data, out=out.data)








class LinearOperator(Operator, FutureField):

    def __init__(self, arg, **kw):

        self.arg = arg
        self.args = [arg]
        self.original_args = [arg]
        self.kw = kw

        self.bases = self._build_bases(arg, **kw)
        self.domain = arg.domain
        self.subdomain = Subdomain.from_bases(self.domain, self.bases)


        self._grid_layout = self.domain.dist.grid_layout
        self._coeff_layout = self.domain.dist.coeff_layout
        self.last_id = None
        self.scales = self.subdomain.dealias
        self.out = None

    def __repr__(self):
        return '{!s}({!r}, {!r})'.format(self.base.__name__, self.arg, self.kw)

    def __str__(self):
        return '{!s}({!s}, {!s})'.format(self.base.__name__, self.arg, self.kw)

    def new_arg(self, arg):
        return self.base(arg, **self.kw)

    def expand(self, *vars):
        """Distribute over sums containing specified variables (default: all)."""
        from . import arithmetic
        if (not vars) or self.arg.has(*vars):
            arg = self.arg.expand(*vars)
            if isinstance(arg, arithmetic.Add):
                arg_a, arg_b = arg.args
                return (self.new_arg(arg_a) + self.new_arg(arg_b)).expand(*vars)
        return self

    def canonical_linear_form(self, *vars):
        """Change argument to canonical linear form."""
        if self.arg.has(*vars):
            return self.new_arg(self.arg.canonical_linear_form(*vars))
        else:
            return self

    def split(self, *vars):
        if any([issubclass(self.base, var) for var in vars]):
            return [self, 0]
        else:
            return [self.new_arg(arg) for arg in self.arg.split(*vars)]

    def subproblem_matrices(self, subproblem, vars, **kw):
        """Build expression matrices acting on subproblem group data."""
        mat0 = self.subproblem_matrix(subproblem)
        mat1 = self.arg.subproblem_matrices(subproblem, vars, **kw)
        return {var: mat0*mat1[var] for var in mat1}


class LinearSubspaceOperator(LinearOperator, FutureField):

    def _build_bases(self, arg, **kw):
        axis = self.space.axis
        bases = [b for b in arg.bases]
        bases[axis] = self.output_basis(self.space, arg.bases[axis])
        return tuple(bases)

    @CachedAttribute
    def space(self):
        return self.arg.domain.get_space_object(self.kw['space'])

    @CachedAttribute
    def basis(self):
        return self.arg.bases[self.space.axis]

    @CachedAttribute
    def axis(self):
        return self.space.axis

    def separability(self, vars):
        """Determine dimensional separability with respect to vars."""
        separability = self.arg.separability(vars).copy()
        if not self.separable:
            separability[self.axis] = False
        return separability

    def sym_diff(self, var):
        """Symbolically differentiate with respect to var."""
        return self.new_arg(self.arg.sym_diff(var))

    @classmethod
    def _build_subspace_matrix(cls, space, **kw):
        dtype = space.domain.dtype
        N = space.coeff_size
        M = sparse.lil_matrix((N, N), dtype=dtype)
        for i in range(N):
            for b in cls.bands:
                j = i + b
                if (0 <= j < N):
                    Mij = cls.entry(i, j, space, **kw)
                    if Mij:
                        M[i,j] = Mij
        return M.tocsr()

    @CachedAttribute
    def subspace_matrix(self):
        """Build matrix operating on subspace data."""
        return self._build_subspace_matrix(self.space, self.basis)

    def subproblem_matrix(self, subproblem):
        """Build operator matrix acting on subproblem group data."""
        shape = subproblem.group_shape(self.subdomain)
        argslice = subproblem.global_slices(self.arg.subdomain)[self.axis]
        outslice = subproblem.global_slices(self.subdomain)[self.axis]
        ax_mats = [sparse.identity(n, format='csr') for n in shape]
        ax_mats[self.axis] = self.subspace_matrix[outslice, argslice]
        return reduce(sparse.kron, ax_mats, 1).tocsr()

    def check_conditions(self):
        layout = self.args[0].layout
        is_coeff = not layout.grid_space[self.space.axis]
        is_local = layout.local[self.space.axis]
        if self.separable:
            return is_coeff
        else:
            return (is_coeff and is_local)

    def enforce_conditions(self):
        self.args[0].require_coeff_space(self.space.axis)
        if not self.separable:
            self.args[0].require_local(self.space.axis)

    def operate(self, out):
        arg0, = self.args
        axis = self.space.axis
        # Apply matrix form
        out.set_layout(arg0.layout)
        matrix = self.subspace_matrix
        if self.separable:
            elements = arg0.layout.local_elements(arg0.subdomain, arg0.scales)[axis]
            matrix = matrix[elements[:,None], elements[None,:]]
        apply_matrix(matrix, arg0.data, axis, out=out.data)


class LinearSubspaceFunctional(LinearSubspaceOperator):

    output_basis_type = None
    separable = False

    def check_conditions(self):
        layout = self.args[0].layout
        is_coeff = not layout.grid_space[self.axis]
        is_local = layout.local[self.axis]
        return (is_coeff and is_local)

    def enforce_conditions(self):
        self.args[0].require_coeff_space(self.axis)
        self.args[0].require_local(self.axis)

    @classmethod
    def _build_subspace_matrix(cls, space, **kw):
        dtype = space.domain.dtype
        N = space.coeff_size
        M = sparse.lil_matrix((1, N), dtype=dtype)
        for j in range(N):
            Mij = cls.entry(j, space, **kw)
            if Mij:
                M[0,j] = Mij
        return M.tocsr()
        # M = np.zeros((1,J), dtype=self.domain.dtype)
        # for j in range(J):
        #     M[0,j] = entry_scaling * self.entry(j, **self.kw)
        # return M

    def output_basis(self, space, basis):
        return None


class TimeDerivative(LinearOperator, FutureField):

    name = 'dt'

    def __new__(cls, arg):
        if isinstance(arg, Number):
            return 0
        else:
            return object.__new__(cls)

    def build_bases(self, arg, **kw):
        return arg.bases

    def separability(self, vars):
        """Determine dimensional separability with respect to vars."""
        return self.arg.separability(vars).copy()


@parseable('interpolate', 'interp')
def interpolate(arg, **positions):
    # Identify domain
    domain = unify_attributes((arg,)+tuple(positions), 'domain', require=False)
    # Apply iteratively
    for space, position in positions.items():
        space = domain.get_space_object(space)
        arg = Interpolate(arg, space, position)
    return arg


class Interpolate(LinearSubspaceFunctional, metaclass=MultiClass):

    @classmethod
    def _check_args(cls, arg, space, position):
        # Dispatch by argument basis
        if isinstance(arg, (Field, FutureField)):
            if space in arg.subdomain:
                if type(arg.get_basis(space)) is cls.input_basis_type:
                    return True
        return False

    def __init__(self, arg, space, position):
        # Wrap initialization to define keywords
        super().__init__(arg, space=space, position=position)
        self.position = position

    @property
    def base(self):
        return Interpolate

    @property
    def subspace_matrix(self):
        """Build matrix operating on subspace data."""
        return self._build_subspace_matrix(self.space, self.basis, self.position)


class InterpolateConstant(Interpolate):

    @classmethod
    def _check_args(cls, arg, space, position):
        if isinstance(arg, Number):
            return True
        elif isinstance(arg, (Field, FutureField)):
            if arg.get_basis(space) is None:
                return True
        return False

    def __new__(cls, arg, space, position):
        return arg


@parseable('integrate', 'integ')
def integrate(arg, *spaces):
    # Identify domain
    domain = unify_attributes((arg,)+spaces, 'domain', require=False)
    # Apply iteratively
    for space in spaces:
        space = domain.get_space_object(space)
        arg = Integrate(arg, space)
    return arg


class Integrate(LinearSubspaceFunctional, metaclass=MultiClass):

    @classmethod
    def _check_args(cls, arg, space):
        # Dispatch by argument basis
        if isinstance(arg, (Field, FutureField)):
            if space in arg.subdomain:
                if type(arg.get_basis(space)) is cls.input_basis_type:
                    return True
        return False

    def __init__(self, arg, space):
        # Wrap initialization to define keywords
        super().__init__(arg, space=space)

    @property
    def base(self):
        return Integrate

    @property
    def subspace_matrix(self):
        """Build matrix operating on subspace data."""
        return self._build_subspace_matrix(self.space, self.basis)


class IntegrateConstant(Integrate):

    @classmethod
    def _check_args(cls, arg, space):
        if isinstance(arg, Number):
            return True
        elif isinstance(arg, (Field, FutureField)):
            if arg.get_basis(space) is None:
                return True
        return False

    def __new__(cls, arg, space):
        return (space.COV.problem_length * arg)


@parseable('filter', 'f')
def filter(arg, **modes):
    # Identify domain
    domain = unify_attributes((arg,)+tuple(modes), 'domain', require=False)
    # Apply iteratively
    for space, mode in modes.items():
        space = domain.get_space_object(space)
        arg = Filter(arg, space, mode)
    return arg


class Filter(LinearSubspaceFunctional):

    def __new__(cls, arg, space, mode):
        if isinstance(arg, Number) or (arg.get_basis(space) is None):
            if mode == 0:
                return arg
            else:
                return 0
        elif space not in arg.subdomain:
            raise ValueError("Invalid space.")
        else:
            return object.__new__(cls)

    def __init__(self, arg, space, mode):
        # Wrap initialization to define keywords
        super().__init__(arg, space=space, mode=mode)

    @property
    def base(self):
        return Filter

    @classmethod
    def entry(cls, j, space, mode):
        """F(j,m) = δ(j,m)"""
        if j == mode:
            return 1
        else:
            return 0


@prefix('d')
@parseable('differentiate', 'diff', 'd')
def differentiate(arg, *spaces, **space_kw):
    # Parse space/order keywords into space list
    for space, order in space_kw.items():
        spaces += (space,) * order
    # Identify domain
    domain = unify_attributes((arg,)+spaces, 'domain', require=False)
    # Apply iteratively
    for space in spaces:
        space = domain.get_space_object(space)
        arg = Differentiate(arg, space)
    return arg


class Differentiate(LinearSubspaceOperator, metaclass=MultiClass):
    """Differentiation along one dimension."""

    def __str__(self):
        return 'd{!s}({!s})'.format(self.space.name, self.arg)

    @classmethod
    def _check_args(cls, arg, space):
        # Dispatch by argument basis
        if isinstance(arg, (Field, FutureField)):
            if space in arg.subdomain:
                if type(arg.get_basis(space)) is cls.input_basis_type:
                    return True
        return False

    def __init__(self, arg, space):
        # Wrap initialization to define keywords
        super().__init__(arg, space=space)

    @CachedAttribute
    def subspace_matrix(self):
        return self._build_subspace_matrix(self.space, self.basis)

    @property
    def base(self):
        return Differentiate

    def expand(self, *vars):
        """Distribute over sums and apply the product rule to arguments
        containing specified variables (default: all)."""
        from . import arithmetic
        arg, = self.args
        space = self.space
        if (not vars) or arg.has(*vars):
            base = self.base
            arg = arg.expand(*vars)
            # Distribute over addition
            if isinstance(arg, arithmetic.Add):
                arg_a, arg_b = arg.args
                return (base(arg_a, space) + base(arg_b, space)).expand(*vars)
            # Apply product rule over multiplication
            if isinstance(arg, arithmetic.Multiply):
                arg_a, arg_b = arg.args
                return (base(arg_a, space)*arg_b + arg_a*base(arg_b, space)).expand(*vars)
        return self


class DifferentiateConstant(Differentiate):

    @classmethod
    def _check_args(cls, arg, space):
        if isinstance(arg, Number):
            return True
        elif isinstance(arg, (Field, FutureField)):
            if arg.get_basis(space) is None:
                return True
        return False

    def __new__(cls, arg, space):
        return 0


@prefix('H')
@parseable('hilbert_transform', 'hilbert', 'H')
def hilbert_transform(arg, *spaces, **space_kw):
    # Parse space/order keywords into space list
    for space, order in space_kw.items():
        spaces += (space,) * order
    # Identify domain
    domain = unify_attributes((arg,)+spaces, 'domain', require=False)
    # Apply iteratively
    for space in spaces:
        space = domain.get_space_object(space)
        arg = HilbertTransform(arg, space)
    return arg


class HilbertTransform(LinearSubspaceOperator):

    @classmethod
    def _check_args(cls, arg, space):
        # Dispatch by argument basis
        if isinstance(arg, (Field, FutureField)):
            if space in arg.subdomain:
                if type(arg.get_basis(space)) is cls.input_basis_type:
                    return True
        return False

    def __init__(self, arg, space):
        # Wrap initialization to define keywords
        super().__init__(arg, space=space)

    @property
    def base(self):
        return HilbertTransform


class HilbertTransformConstant(HilbertTransform):

    @classmethod
    def _check_args(cls, arg, space):
        if isinstance(arg, Number):
            return True
        elif isinstance(arg, (Field, FutureField)):
            if arg.get_basis(space) is None:
                return True
        return False

    def __new__(cls, arg, space):
        return 0


def convert(arg, bases):
    # Drop Nones
    bases = [b for b in bases if b is not None]
    # if not bases:
    #     return arg
    # # Cast to operand
    # domain = unify_attributes(bases, 'domain', require=False)
    # arg = Field.cast(arg, domain=domain)
    # Apply iteratively
    for basis in bases:
        arg = Convert(arg, basis)
    return arg


class Convert(LinearSubspaceOperator, metaclass=MultiClass):

    def __str__(self):
        return str(self.arg)
        #return 'C{!s}({!s})'.format(self.space.name, self.arg)

    @classmethod
    def _check_args(cls, arg, basis):
        # Dispatch by argument and target bases
        if isinstance(arg, (Field, FutureField)):
            if basis in arg.bases:
                return False
            elif basis.space in arg.subdomain.spaces:
                input_basis = arg.get_basis(basis.space)
                if type(input_basis) is not cls.input_basis_type:
                    return False
                if type(basis) is not cls.output_basis_type:
                    return False
                return True
        return False

    def __init__(self, arg, basis):
        # Wrap initialization to define keywords
        self._output_basis = basis
        arg = Field.cast(arg, domain=basis.domain)
        super().__init__(arg, basis=basis)

    def output_basis(self, space, input_basis):
        return self._output_basis

    @CachedAttribute
    def subspace_matrix(self):
        space = self.space
        input_basis = self.basis
        output_basis = self._output_basis
        return self._build_subspace_matrix(space, input_basis, output_basis)

    @property
    def base(self):
        return Convert

    @CachedAttribute
    def space(self):
        return self.kw['basis'].space


class ConvertSame(Convert):
    """Trivial conversion to same basis."""

    @classmethod
    def _check_args(cls, arg, basis):
        if isinstance(arg, (Field, FutureField)):
            if basis in arg.bases:
                return True
        return False

    def __new__(cls, arg, basis):
        return arg


class ConvertConstant(Convert):
    """Conversion up from a constant."""

    separable = True
    bands = [0]

    @classmethod
    def _check_args(cls, arg, basis):
        if 0 in basis.modes:
            if isinstance(arg, Number):
                return True
            elif isinstance(arg, (Field, FutureField)):
                input_basis = arg.get_basis(basis.space)
                if input_basis is None:
                    return True
        return False

    def __init__(self, arg, basis):
        arg = Operand.cast(arg, basis.domain)
        super().__init__(arg, basis)

    def build_bases(self, arg, **kw):
        bases = [b for b in arg.bases]
        bases[self.space.axis] = self.kw['basis']
        return tuple(bases)

    @classmethod
    def entry(cls, i, j, space, basis):
        if i == j == 0:
            return (1 / basis.const)
        else:
            return 0

    def check_conditions(self):
        return True

    def enforce_conditions(self):
        pass

    def operate(self, out):
        arg = self.args[0]
        axis = self.space.axis
        out.set_layout(arg.layout)
        if arg.layout.grid_space[axis]:
            # Broadcast addition
            np.copyto(out.data, arg.data)
        else:
            # Set constant mode
            out.data.fill(0)
            if 0 in out.local_elements()[axis]:
                np.copyto(out.data[axslice(axis, 0, 1)], arg.data / self._output_basis.const)





