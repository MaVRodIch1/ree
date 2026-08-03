"""
Pure-Python drop-in for the parts of TgCrypto that opentele needs
(AES-256-IGE), so tdata conversion works without a C compiler.

opentele hard-imports `tgcrypto` and calls ige256_encrypt/decrypt. The real
TgCrypto is a C extension with no prebuilt wheels for recent Python on
Windows, forcing a Visual C++ build. This module lives next to
tdata_convert.py, so `import tgcrypto` resolves here first. Backed by
pycryptodome's AES-ECB, implementing standard AES-IGE (the same scheme
TgCrypto/Telegram use).
"""
from Crypto.Cipher import AES


def _xor(a, b):
    return bytes(x ^ y for x, y in zip(a, b))


def ige256_encrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    # opentele passes PyQt5 QByteArrays; coerce to real bytes so element
    # iteration yields ints, not length-1 byte objects.
    data, key, iv = bytes(data), bytes(key), bytes(iv)
    cipher = AES.new(key, AES.MODE_ECB)
    prev_c, prev_p = iv[:16], iv[16:32]
    out = bytearray()
    for i in range(0, len(data), 16):
        p = data[i:i + 16]
        c = _xor(cipher.encrypt(_xor(p, prev_c)), prev_p)
        out += c
        prev_c, prev_p = c, p
    return bytes(out)


def ige256_decrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    data, key, iv = bytes(data), bytes(key), bytes(iv)
    cipher = AES.new(key, AES.MODE_ECB)
    prev_c, prev_p = iv[:16], iv[16:32]
    out = bytearray()
    for i in range(0, len(data), 16):
        c = data[i:i + 16]
        p = _xor(cipher.decrypt(_xor(c, prev_p)), prev_c)
        out += p
        prev_c, prev_p = c, p
    return bytes(out)
