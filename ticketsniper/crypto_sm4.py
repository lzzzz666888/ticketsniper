# -*- coding: utf-8 -*-
"""纯 Python SM4（GB/T 32907-2016）+ ECB/CBC，用于 12306 密码登录。

12306 登录页 js/framework/SM4.js 的 encrypt_ecb(plaintext, key) 语义：
  - key 按 UTF-8 取字节（16 字节）
  - 明文 PKCS#7 填充到 16 的倍数
  - ECB 逐块加密
  - 输出 base64

官方 JS 注释里的测试向量：
  encrypt_ecb("1234", "1234567890123456") => "woPrxebr8Xvyo1qG8QxAUA=="
本模块 self_test() 会核对该向量，登录前可先跑一遍确认实现正确。

密码字段协议：
  formData['password'] = '@' + encrypt_ecb(明文密码, 'tiekeyuankp12306')
"""
from __future__ import annotations

import base64
import struct
from typing import List

# ---------------------------------------------------------------- 常量表
SBOX = bytes([
    0xd6, 0x90, 0xe9, 0xfe, 0xcc, 0xe1, 0x3d, 0xb7, 0x16, 0xb6, 0x14, 0xc2, 0x28, 0xfb, 0x2c, 0x05,
    0x2b, 0x67, 0x9a, 0x76, 0x2a, 0xbe, 0x04, 0xc3, 0xaa, 0x44, 0x13, 0x26, 0x49, 0x86, 0x06, 0x99,
    0x9c, 0x42, 0x50, 0xf4, 0x91, 0xef, 0x98, 0x7a, 0x33, 0x54, 0x0b, 0x43, 0xed, 0xcf, 0xac, 0x62,
    0xe4, 0xb3, 0x1c, 0xa9, 0xc9, 0x08, 0xe8, 0x95, 0x80, 0xdf, 0x94, 0xfa, 0x75, 0x8f, 0x3f, 0xa6,
    0x47, 0x07, 0xa7, 0xfc, 0xf3, 0x73, 0x17, 0xba, 0x83, 0x59, 0x3c, 0x19, 0xe6, 0x85, 0x4f, 0xa8,
    0x68, 0x6b, 0x81, 0xb2, 0x71, 0x64, 0xda, 0x8b, 0xf8, 0xeb, 0x0f, 0x4b, 0x70, 0x56, 0x9d, 0x35,
    0x1e, 0x24, 0x0e, 0x5e, 0x63, 0x58, 0xd1, 0xa2, 0x25, 0x22, 0x7c, 0x3b, 0x01, 0x21, 0x78, 0x87,
    0xd4, 0x00, 0x46, 0x57, 0x9f, 0xd3, 0x27, 0x52, 0x4c, 0x36, 0x02, 0xe7, 0xa0, 0xc4, 0xc8, 0x9e,
    0xea, 0xbf, 0x8a, 0xd2, 0x40, 0xc7, 0x38, 0xb5, 0xa3, 0xf7, 0xf2, 0xce, 0xf9, 0x61, 0x15, 0xa1,
    0xe0, 0xae, 0x5d, 0xa4, 0x9b, 0x34, 0x1a, 0x55, 0xad, 0x93, 0x32, 0x30, 0xf5, 0x8c, 0xb1, 0xe3,
    0x1d, 0xf6, 0xe2, 0x2e, 0x82, 0x66, 0xca, 0x60, 0xc0, 0x29, 0x23, 0xab, 0x0d, 0x53, 0x4e, 0x6f,
    0xd5, 0xdb, 0x37, 0x45, 0xde, 0xfd, 0x8e, 0x2f, 0x03, 0xff, 0x6a, 0x72, 0x6d, 0x6c, 0x5b, 0x51,
    0x8d, 0x1b, 0xaf, 0x92, 0xbb, 0xdd, 0xbc, 0x7f, 0x11, 0xd9, 0x5c, 0x41, 0x1f, 0x10, 0x5a, 0xd8,
    0x0a, 0xc1, 0x31, 0x88, 0xa5, 0xcd, 0x7b, 0xbd, 0x2d, 0x74, 0xd0, 0x12, 0xb8, 0xe5, 0xb4, 0xb0,
    0x89, 0x69, 0x97, 0x4a, 0x0c, 0x96, 0x77, 0x7e, 0x65, 0xb9, 0xf1, 0x09, 0xc5, 0x6e, 0xc6, 0x84,
    0x18, 0xf0, 0x7d, 0xec, 0x3a, 0xdc, 0x4d, 0x20, 0x79, 0xee, 0x5f, 0x3e, 0xd7, 0xcb, 0x39, 0x48,
])

FK = [0xa3b1bac6, 0x56aa3350, 0x677d9197, 0xb27022dc]

CK = [
    0x00070e15, 0x1c232a31, 0x383f464d, 0x545b6269,
    0x70777e85, 0x8c939aa1, 0xa8afb6bd, 0xc4cbd2d9,
    0xe0e7eef5, 0xfc030a11, 0x181f262d, 0x343b4249,
    0x50575e65, 0x6c737a81, 0x888f969d, 0xa4abb2b9,
    0xc0c7ced5, 0xdce3eaf1, 0xf8ff060d, 0x141b2229,
    0x30373e45, 0x4c535a61, 0x686f767d, 0x848b9299,
    0xa0a7aeb5, 0xbcc3cad1, 0xd8dfe6ed, 0xf4fb0209,
    0x10171e25, 0x2c333a41, 0x484f565d, 0x646b7279,
]

_MASK = 0xFFFFFFFF


def _rotl(x: int, n: int) -> int:
    n &= 31
    return ((x << n) | (x >> (32 - n))) & _MASK


def _tau(a: int) -> int:
    return (SBOX[(a >> 24) & 0xFF] << 24) | (SBOX[(a >> 16) & 0xFF] << 16) | \
           (SBOX[(a >> 8) & 0xFF] << 8) | SBOX[a & 0xFF]


def _t_transform(x: int) -> int:
    b = _tau(x)
    return b ^ _rotl(b, 2) ^ _rotl(b, 10) ^ _rotl(b, 18) ^ _rotl(b, 24)


def _t_key(x: int) -> int:
    b = _tau(x)
    return b ^ _rotl(b, 13) ^ _rotl(b, 23)


def _round_keys(key: bytes) -> List[int]:
    if len(key) != 16:
        raise ValueError("SM4 key 必须 16 字节")
    k = list(struct.unpack(">4I", key))
    for i in range(4):
        k[i] ^= FK[i]
    rk: List[int] = []
    for i in range(32):
        t = k[i + 1] ^ k[i + 2] ^ k[i + 3] ^ CK[i]
        nk = k[i] ^ _t_key(t)
        k.append(nk)
        rk.append(nk)
    return rk


def _crypt_block(block: bytes, rk: List[int]) -> bytes:
    x = list(struct.unpack(">4I", block))
    for i in range(32):
        x.append(x[i] ^ _t_transform(x[i + 1] ^ x[i + 2] ^ x[i + 3] ^ rk[i]))
    out = (x[35], x[34], x[33], x[32])
    return struct.pack(">4I", *out)


# ---------------------------------------------------------------- 填充
def pkcs7_pad(data: bytes, block: int = 16) -> bytes:
    n = block - len(data) % block
    return data + bytes([n]) * n


def pkcs7_unpad(data: bytes) -> bytes:
    if not data:
        return data
    n = data[-1]
    if 1 <= n <= 16 and data[-n:] == bytes([n]) * n:
        return data[:-n]
    return data


# ---------------------------------------------------------------- 对外接口
def encrypt_ecb_raw(plaintext: bytes, key: bytes) -> bytes:
    rk = _round_keys(key)
    data = pkcs7_pad(plaintext)
    out = bytearray()
    for i in range(0, len(data), 16):
        out += _crypt_block(data[i:i + 16], rk)
    return bytes(out)


def decrypt_ecb_raw(ciphertext: bytes, key: bytes) -> bytes:
    rk = _round_keys(key)[::-1]
    out = bytearray()
    for i in range(0, len(ciphertext) - len(ciphertext) % 16, 16):
        out += _crypt_block(ciphertext[i:i + 16], rk)
    return pkcs7_unpad(bytes(out))


def encrypt_ecb(plaintext: str, key: str) -> str:
    """与 JS encrypt_ecb 完全一致：返回 base64。"""
    return base64.b64encode(
        encrypt_ecb_raw(plaintext.encode("utf-8"), key.encode("utf-8"))
    ).decode()


def encrypt_cbc(plaintext: str, key: str, iv: str) -> str:
    rk = _round_keys(key.encode("utf-8"))
    data = pkcs7_pad(plaintext.encode("utf-8"))
    prev = iv.encode("utf-8")
    out = bytearray()
    for i in range(0, len(data), 16):
        blk = bytes(a ^ b for a, b in zip(data[i:i + 16], prev))
        prev = _crypt_block(blk, rk)
        out += prev
    return base64.b64encode(bytes(out)).decode()


def password_field(plain: str, key: str = "tiekeyuankp12306") -> str:
    """12306 登录表单里的 password 值：'@' + SM4-ECB-base64。"""
    return "@" + encrypt_ecb(plain, key)


# ---------------------------------------------------------------- 自检
VECTOR_PLAIN = "1234"
VECTOR_KEY = "1234567890123456"
VECTOR_EXPECT = "woPrxebr8Xvyo1qG8QxAUA=="
# GB/T 32907 标准向量（单块、无填充）
STD_KEY = bytes.fromhex("0123456789abcdeffedcba9876543210")
STD_PLAIN = bytes.fromhex("0123456789abcdeffedcba9876543210")
STD_CIPHER = bytes.fromhex("681edf34d206965e86b3e94f536e4246")


def self_test() -> bool:
    """返回 True 表示实现与官方 JS/国标一致。"""
    ok = True
    rk = _round_keys(STD_KEY)
    got = _crypt_block(STD_PLAIN, rk)
    if got != STD_CIPHER:
        ok = False
    if encrypt_ecb(VECTOR_PLAIN, VECTOR_KEY) != VECTOR_EXPECT:
        ok = False
    back = decrypt_ecb_raw(
        encrypt_ecb_raw("中文密码测试pwd!@#".encode(), VECTOR_KEY.encode()), VECTOR_KEY.encode()
    )
    if back.decode() != "中文密码测试pwd!@#":
        ok = False
    return ok


if __name__ == "__main__":
    print("std block :", _crypt_block(STD_PLAIN, _round_keys(STD_KEY)).hex())
    print("js vector :", encrypt_ecb(VECTOR_PLAIN, VECTOR_KEY), "expect", VECTOR_EXPECT)
    print("password  :", password_field("MyPassw0rd"))
    print("self_test :", self_test())
