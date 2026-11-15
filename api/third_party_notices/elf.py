"""Just enough ELF64 (little-endian) to read a shared object's dynamic symbol table.

Statically linked code shows in no package metadata and in no `*.libs` directory; what it leaves behind is
the symbols it defines. Reading `.dynsym` (defined GLOBAL/WEAK symbols only — an undefined reference means
the code lives elsewhere) is how the drift check sees it.
"""
import struct

SHT_DYNSYM = 11
_SH = struct.Struct("<IIQQQQIIQQ")    # name type flags addr offset size link info addralign entsize
_SYM = struct.Struct("<IBBHQQ")       # name info other shndx value size
STB_GLOBAL, STB_WEAK = 1, 2


def _sections(data):
    if len(data) < 64 or data[:4] != b"\x7fELF" or data[4] != 2 or data[5] != 1:
        return None
    shoff, = struct.unpack_from("<Q", data, 0x28)
    shentsize, shnum, _ = struct.unpack_from("<HHH", data, 0x3A)
    if shentsize != _SH.size or shoff + shnum * shentsize > len(data):
        return None
    return [_SH.unpack_from(data, shoff + i * shentsize) for i in range(shnum)]


def _cstr(data, off):
    end = data.index(b"\0", off)
    return data[off:end].decode("latin-1")


def _defined(data, secs):
    """Yields (name, shndx, value, size) for every defined GLOBAL/WEAK dynamic symbol."""
    for sec in secs:
        if sec[1] != SHT_DYNSYM:
            continue
        strtab = secs[sec[6]]
        for i in range(sec[5] // _SYM.size):
            name, info, _other, shndx, value, size = _SYM.unpack_from(data, sec[4] + i * _SYM.size)
            if name and shndx != 0 and (info >> 4) in (STB_GLOBAL, STB_WEAK):
                yield _cstr(data, strtab[4] + name), shndx, value, size


def defined_symbols(path):
    """The set of defined dynamic symbol names, or None when the file is not an ELF64 LE object."""
    data = open(path, "rb").read()
    secs = _sections(data)
    if secs is None:
        return None
    return {name for name, *_ in _defined(data, secs)}


def symbol_bytes(path, symbol):
    """The bytes of a defined data symbol (e.g. a version integer), or None when absent."""
    data = open(path, "rb").read()
    secs = _sections(data)
    if secs is None:
        return None
    for name, shndx, value, size in _defined(data, secs):
        if name == symbol and shndx < len(secs):
            sec = secs[shndx]
            off = sec[4] + (value - sec[3])
            return data[off:off + size]
    return None
