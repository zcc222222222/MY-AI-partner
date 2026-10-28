"""Explicit ownership for event iterators, including non-generator adapters."""
from contextlib import contextmanager


@contextmanager
def closing_iterator(iterator):
    try:
        yield iterator
    finally:
        close = getattr(iterator, 'close', None)
        if close is not None:
            close()
