# Copyright (C) 2016 - 2025 ANSYS, Inc. and/or its affiliates.
# SPDX-License-Identifier: MIT
#
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

"""Common gRPC functions"""
import time
from typing import Dict, Iterable, List, Literal, Optional, get_args

from ansys.api.mapdl.v0 import ansys_kernel_pb2 as anskernel
import grpc
import numpy as np

from ansys.mapdl.core import LOG
from ansys.mapdl.core.errors import MapdlConnectionError, MapdlRuntimeError

# chunk sizes for streaming and file streaming
DEFAULT_CHUNKSIZE: int = 256 * 1024  # 256 kB
DEFAULT_FILE_CHUNK_SIZE: int = 1024 * 1024  # 1MB

ANSYS_VALUE_TYPE: Dict[int, Optional[np.typing.DTypeLike]] = {
    0: None,  # UNKNOWN
    1: np.int32,  # INTEGER
    2: np.int64,  # HYPER
    3: np.int16,  # SHORT
    4: np.float32,  # FLOAT
    5: np.float64,  # DOUBLE
    6: np.complex64,  # FCPLX
    7: np.complex128,  # DCPLX
    8: np.char,
}


VGET_ENTITY_TYPES_TYPING: List[str] = Literal[
    "NODE",
    "ELEM",
    "KP",
    "LINE",
    "AREA",
    "VOLU",
    "CDSY",
    "RCON",
    "TLAB",
]

VGET_ENTITY_TYPES: List[str] = list(get_args(VGET_ENTITY_TYPES_TYPING))

STRESS_TYPES: List[str] = ["X", "Y", "Z", "XY", "YZ", "XZ", "1", "2", "3", "INT", "EQV"]
COMP_TYPE: List[str] = ["X", "Y", "Z", "SUM"]
VGET_NODE_ENTITY_TYPES: Dict[str, List[str]] = {
    "U": ["X", "Y", "Z"],
    "S": STRESS_TYPES,
    "EPTO": STRESS_TYPES,
    "EPEL": STRESS_TYPES,
    "EPPL": STRESS_TYPES,
    "EPCR": STRESS_TYPES,
    "EPTH": STRESS_TYPES,
    "EPDI": STRESS_TYPES,
    "EPSW": [None],
    "NL": ["SEPL", "SRAT", "HPRES", "EPEQ", "PSV", "PLWK"],
    "HS": ["X", "Y", "Z"],
    "BFE": ["TEMP"],
    "TG": COMP_TYPE,
    "TF": COMP_TYPE,
    "PG": COMP_TYPE,
    "EF": COMP_TYPE,
    "D": COMP_TYPE,
    "H": COMP_TYPE,
    "B": COMP_TYPE,
    "FMAG": COMP_TYPE,
    "NLIST": [None],
}


class GrpcError(MapdlRuntimeError):
    """Raised when gRPC fails"""

    def __init__(self, msg: str = "") -> None:
        super().__init__(self, msg)


def check_vget_input(entity: str, item: str, itnum: str) -> str:
    """Verify that entity and item for VGET are valid.

    Raises a ``ValueError`` when invalid.

    Parameters
    ----------
    entity : str
        Entity keyword. Valid keywords are:

        - ``'NODE'``
        - ``'ELEM'``
        - ``'KP'``
        - ``'LINE'``
        - ``'AREA'``
        - ``'VOLU'``
        - ``'CDSY'``
        - ``'RCON'``
        - ``'TLAB'``

    item : str
        The name of a particular item for the given entity. Valid
        items are as shown in the item columns of the tables
        within the ``*VGET`` command reference in your ANSYS manual.

    itnum : str
        The number (or label) for the specified item (if
        any). Valid it1num values are as shown in the IT1NUM
        columns of the tables in the command reference section for
        the ``*VGET`` command in your ANSYS manual. Some Item1 labels
        do not require an IT1NUM value.

    Returns
    -------
    str
        MAPDL formatted vget command after the "VGET, " in the format of:
        "ENTITY, , ITEM, ITNUM"
    """
    entity = entity.upper()
    if item is not None:
        item = item.upper()

    if itnum is not None:
        itnum = itnum.upper()

    if entity not in VGET_ENTITY_TYPES:
        raise ValueError(
            'Entity "%s" not allowed.  Allowed items:\n%s' % entity,
            str(VGET_ENTITY_TYPES),
        )

    if entity == "NODE":
        if item not in VGET_NODE_ENTITY_TYPES:
            allowed_types = list(VGET_NODE_ENTITY_TYPES.keys())
            raise ValueError(
                'item "%s" for "NODE" not allowed.  Allowed items:%s\n'
                % (item, str(allowed_types))
            )

        if itnum not in VGET_NODE_ENTITY_TYPES[item]:
            allowed_types = VGET_NODE_ENTITY_TYPES[item]
            raise ValueError(
                'itnum "%s" for item "%s" not allowed.  Allowed items:\n%s'
                % (itnum, item, str(allowed_types))
            )

    # None is not allowed in MAPDL commands
    if item is None:
        item = ""
    if itnum is None:
        itnum = ""

    return "%s, , %s, %s" % (entity, item, itnum)


def parse_chunks(
    chunks: Iterable[anskernel.Chunk], dtype: Optional[np.typing.DTypeLike] = None
) -> np.ndarray:
    """Deserialize gRPC chunks into a numpy array

    Parameters
    ----------
    chunks : generator
        generator from grpc.  Each chunk contains a bytes payload

    dtype : np.dtype
        Numpy data type to interpret chunks as.

    Returns
    -------
    np.ndarray
        Deserialized numpy array.

    """
    timeout = 3  # seconds
    time_step = 0.01
    time_max = timeout + time.time()  # seconds

    while not chunks.is_active() and time.time() < time_max:
        time.sleep(time_step)

    if not chunks.is_active() and chunks.code() != grpc.StatusCode.OK:
        LOG.error("The channel might not alive.")

    try:
        chunk = chunks.next()

    except StopIteration:
        if chunks.code() == grpc.StatusCode.OK:
            return np.empty(0)
        else:
            raise MapdlConnectionError(
                "The chunk couldn't be parsed. The error information is:\n"
                f"code: {chunks.code()}\n"
                f"message: '{chunks.details()}'"
            )

    if not chunk.value_type and dtype is None:
        raise ValueError("Must specify a data type for this record")

    if dtype is None:
        dtype = ANSYS_VALUE_TYPE[chunk.value_type]

    array = np.frombuffer(chunk.payload, dtype)
    if chunks.done():
        return array

    arrays = [array]
    for chunk in chunks:
        arrays.append(np.frombuffer(chunk.payload, dtype))

    return np.hstack(arrays)
