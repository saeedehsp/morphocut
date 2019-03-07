from abc import abstractmethod, ABC

__all__ = ["NodeBase", "SimpleNodeBase"]


class NodeBase(ABC):
    """
    Base class for all pipeline nodes.
    """
    @abstractmethod
    def __call__(self, input=None):  # pragma: no cover
        """
        Process the input stream
        """
        while False:
            yield None


class SimpleNodeBase(NodeBase):
    """
    Base class for simple pipeline nodes that operate on each object individually.
    """

    def __init__(self, input_facet, output_facet):
        self.input_facet = input_facet
        self.output_facet = output_facet

    def __call__(self, input=None):
        for obj in input:
            yield self.process(obj)

    @abstractmethod
    def process(self, obj):
        pass
