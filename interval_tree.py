"""
AVL-balanced interval tree for O(log n + k) overlap queries.
Each node: [low, high) interval + augmented max_high for subtree pruning.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional


@dataclass
class Interval:
    low: float; high: float; event_id: int; title: str = ""; source: str = ""

@dataclass
class _Node:
    iv: Interval; max_high: float
    left: Optional[_Node] = None; right: Optional[_Node] = None; height: int = 1

def _h(n: Optional[_Node]) -> int: return n.height if n else 0
def _mh(n: Optional[_Node]) -> float: return n.max_high if n else float("-inf")

def _up(n: _Node) -> _Node:
    n.height = 1 + max(_h(n.left), _h(n.right))
    n.max_high = max(n.iv.high, _mh(n.left), _mh(n.right))
    return n

def _rot_r(y: _Node) -> _Node:
    x = y.left; y.left = x.right; x.right = y; _up(y); _up(x); return x  # type: ignore

def _rot_l(x: _Node) -> _Node:
    y = x.right; x.right = y.left; y.left = x; _up(x); _up(y); return y  # type: ignore

def _bal(n: _Node) -> _Node:
    _up(n); bf = _h(n.left) - _h(n.right)
    if bf > 1:
        if _h(n.left.left) < _h(n.left.right): n.left = _rot_l(n.left)  # type: ignore
        return _rot_r(n)
    if bf < -1:
        if _h(n.right.right) < _h(n.right.left): n.right = _rot_r(n.right)  # type: ignore
        return _rot_l(n)
    return n


class IntervalTree:
    def __init__(self): self._root: Optional[_Node] = None; self._size = 0

    @property
    def size(self) -> int: return self._size

    def insert(self, iv: Interval):
        self._root = self._ins(self._root, iv); self._size += 1

    def _ins(self, n: Optional[_Node], iv: Interval) -> _Node:
        if not n: return _Node(iv=iv, max_high=iv.high)
        if iv.low <= n.iv.low: n.left = self._ins(n.left, iv)
        else: n.right = self._ins(n.right, iv)
        return _bal(n)

    def query_overlaps(self, lo: float, hi: float) -> list[Interval]:
        r: list[Interval] = []; self._q(self._root, lo, hi, r); return r

    def _q(self, n: Optional[_Node], lo: float, hi: float, r: list[Interval]):
        if not n or n.max_high <= lo: return
        self._q(n.left, lo, hi, r)
        if n.iv.low < hi and n.iv.high > lo: r.append(n.iv)
        if n.iv.low < hi: self._q(n.right, lo, hi, r)

    def delete(self, iv: Interval):
        self._root, ok = self._del(self._root, iv)
        if ok: self._size -= 1

    def _del(self, n: Optional[_Node], iv: Interval) -> tuple[Optional[_Node], bool]:
        if not n: return None, False
        if iv is n.iv:
            if not n.left: return n.right, True
            if not n.right: return n.left, True
            s = n.right
            while s.left: s = s.left
            n.iv = s.iv; n.right, _ = self._del(n.right, s.iv)
            return _bal(n), True
        if iv.low <= n.iv.low:
            n.left, ok = self._del(n.left, iv)
            if not ok: n.right, ok = self._del(n.right, iv)
        else:
            n.right, ok = self._del(n.right, iv)
            if not ok: n.left, ok = self._del(n.left, iv)
        return (_bal(n), True) if ok else (n, False)

    @staticmethod
    def bulk_build(ivs: list[Interval]) -> IntervalTree:
        t = IntervalTree()
        if not ivs: return t
        s = sorted(ivs, key=lambda i: i.low)
        t._root = IntervalTree._bb(s, 0, len(s) - 1); t._size = len(s)
        return t

    @staticmethod
    def _bb(ivs: list[Interval], lo: int, hi: int) -> Optional[_Node]:
        if lo > hi: return None
        mid = (lo + hi) // 2
        n = _Node(iv=ivs[mid], max_high=ivs[mid].high)
        n.left = IntervalTree._bb(ivs, lo, mid - 1)
        n.right = IntervalTree._bb(ivs, mid + 1, hi)
        _up(n); return n
