"""Core components of the MorphoCut processing graph."""

import inspect
import operator
import warnings
from functools import wraps
from typing import Callable, Generic, Tuple, TypeVar, Union

_pipeline_stack = []  # type: ignore, pylint: disable=invalid-name


def _resolve_variable(obj, variable_or_value):
    if isinstance(variable_or_value, Variable):
        return obj[variable_or_value]

    if isinstance(variable_or_value, tuple):
        return tuple(_resolve_variable(obj, v) for v in variable_or_value)

    if isinstance(variable_or_value, dict):
        return {k: _resolve_variable(obj, v) for k, v in variable_or_value.items()}

    return variable_or_value


T = TypeVar('T')


class Variable(Generic[T]):
    __slots__ = ["name", "node"]

    def __init__(self, name, node):
        self.name = name
        self.node = node

    def __getattr__(self, name):
        return LambdaNode(getattr, self, name)

    def __getitem__(self, key):
        return LambdaNode(operator.getitem, self, key)

    def __setitem__(self, key, value):
        return LambdaNode(operator.setitem, self, key, value)


RawOrVariable = Union[T, Variable[T]]
NodeCallReturnType = Union[None, Variable, Tuple[Variable]]


class Node:
    """Represents a node in the computation graph."""

    def __init__(self):
        # Bind outputs to self
        outputs = getattr(self.__class__, "outputs", [])
        self.outputs = [self.__bind_output(o) for o in outputs]
        self._outputs_retrieved = False

        # Register with pipeline
        try:
            # pylint: disable=protected-access
            _pipeline_stack[-1]._add_node(
                self)
        except IndexError:
            raise RuntimeError("Empty pipeline stack") from None

    def __bind_output(self, port):
        """Bind self to port and return a variable."""
        variable = port.create_variable(self)

        return variable

    def __call__(self) -> NodeCallReturnType:
        """Return outputs."""

        try:
            outputs = self.__dict__["outputs"]
        except KeyError:
            raise RuntimeError(
                "'{type}' is not initialized properly. Did you forget a super().__init__() in the constructor?".format(
                    type=type(self).__name__
                )
            )

        self._outputs_retrieved = True

        # Return outputs
        if not outputs:
            return None
        if len(outputs) == 1:
            # If one output, return exactly this
            return outputs[0]
        # Otherwise, return list of outputs
        return outputs

    def prepare_input(self, obj, names):
        """Return a tuple corresponding to the input ports."""

        if isinstance(names, str):
            return _resolve_variable(obj, getattr(self, names))

        return tuple(
            _resolve_variable(obj, v) for v in (getattr(self, n) for n in names)
        )

    def prepare_output(self, obj, *values):
        """Update obj using the values corresponding to the output ports."""

        if not self.outputs:
            if any(values):
                raise ValueError(
                    "No output port specified but transform returned a value."
                )

            return obj

        while True:
            n_values = len(values)
            n_outputs = len(self.outputs)
            if n_values != n_outputs:
                # If values is a nested tuple, unnest and retry
                if n_values == 1 and isinstance(values[0], tuple):
                    values = values[0]
                    continue
                raise ValueError(
                    "Length of values does not match number of output ports: {} vs. {}".format(
                        n_values, n_outputs
                    )
                )
            break

        for variable, r in zip(self.outputs, values):
            obj[variable] = r

        return obj

    def after_stream(self):
        """
        Do something after the stream was processed.

        Called by transform_stream after stream processing is done.
        Override this in your own implementation.
        """

    def _get_parameter_names(self):
        """Inspect self.transform to get the parameter names."""
        return [
            p.name
            for p in inspect.signature(
                self.transform  # pylint: disable=no-member
            ).parameters.values()
            if p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
        ]

    def transform_stream(self, stream):
        """Transform a stream."""

        if not self._outputs_retrieved:
            warnings.warn(
                "Outputs were not retrieved. Did you forget a () after {type}(...)?".format(
                    type=type(self).__name__
                )
            )

        names = self._get_parameter_names()

        for obj in stream:
            parameters = self.prepare_input(obj, names)

            result = self.transform(*parameters)  # pylint: disable=no-member

            self.prepare_output(obj, result)

            yield obj

        self.after_stream()

    def __str__(self):
        return "{}()".format(self.__class__.__name__)


class Output:
    """Stores meta data about a output of a Node.

    This is used as a decorator.

    Example:
        @ReturnOutputs
        @Output("bar")
        class Foo(Node):
            ...

    """

    def __init__(
        self,
        name,
        doc=None
    ):
        self.name = name
        self.doc = doc
        self.node_cls = None

    def create_variable(self, node):
        """Return a _Variable with a reference to the node."""

        return Variable(self.name, node)

    def __repr__(self):
        return '{}("{}", {})'.format(self.__class__.__name__, self.name, self.node_cls)

    def __call__(self, cls):
        """Add this output to the list of a nodes outputs."""

        if not issubclass(cls, Node):
            raise ValueError(
                "This decorator is meant to be applied to a subclass of Node."
            )

        try:
            outputs = cls.outputs
        except AttributeError:
            outputs = cls.outputs = []

        outputs.insert(0, self)

        self.node_cls = cls

        return cls


def ReturnOutputs(node_cls):
    if not issubclass(node_cls, Node):
        raise ValueError(
            "This decorator is meant to be applied to a subclass of Node."
        )

    @wraps(node_cls)
    def wrapper(*args, **kwargs) -> NodeCallReturnType:
        return node_cls(*args, **kwargs)()
    wrapper.node_cls = node_cls
    return wrapper


@ReturnOutputs
@Output("out")
class LambdaNode(Node):
    """
    Apply a function to the supplied variables.

    Args:
        clbl: A callable.
        *args: Positional arguments to clbl.
        **kwargs. Keyword-arguments to clbl.

    Output:
        The result of the function application.

    """

    def __init__(self, clbl: Callable, *args, **kwargs):
        super().__init__()
        self.clbl = clbl
        self.args = args
        self.kwargs = kwargs

    def transform(self, clbl, args, kwargs):
        """Apply clbl to the supplied arguments."""
        return clbl(*args, **kwargs)

    def __str__(self):
        return "{}({})".format(self.__class__.__name__, self.clbl.__name__)


class Pipeline:
    def __init__(self):
        self.nodes = []

    def __enter__(self):
        # Push self to pipeline stack
        _pipeline_stack.append(self)

        return self

    def __exit__(self, *_):
        # Pop self from pipeline stack
        item = _pipeline_stack.pop()

        assert item is self

    def transform_stream(self, stream=None):
        if stream is None:
            stream = [{}]

        for node in self.nodes:
            stream = node.transform_stream(stream)

        return stream

    def run(self):
        for _ in self.transform_stream():
            pass

    def _add_node(self, node):
        self.nodes.append(node)

    def __str__(self):
        return "Pipeline([{}])".format(", ".join(str(n) for n in self.nodes))
