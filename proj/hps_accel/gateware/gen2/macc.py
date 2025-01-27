#!/bin/env python
# Copyright 2021 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Multiply Accumulate Blocks for a systolic array"""


from nmigen import Mux, Signal, unsigned
from nmigen_cfu.util import tree_sum, SimpleElaboratable

from .utils import delay


class MaccBlock(SimpleElaboratable):
    """An N-wide multiply and accumulate block.

    This is intended to be used as a block in a systolic array. It has
    two inputs, A and B. A is input from the left and is passed to the
    right. B is input from the top and passed to the bottom.

    The calcuation performed is:

    accumulator += sum(A[n] * B[n] for n in range N)

    This block provides no flow control mechanisms. It is expected that
    new inputs be provided on every cycle, and that output will be
    processed when it is available. Avoiding flow control mechanisms
    reduces the complexity and area use of this component, at the cost
    of some additional external complexity.

    The A input also carries 'first' and 'last signals' which control
    accumulation.

    This logic is intended to map to the Crosslink/NX SysDSP block.

    Parameters
    ----------

    n: int
        The number of multipliers. Four is the usual value.

    a_shape: Shape
        The shape of each the value packed into the A input. If the A
        input is used for Conv2D activation values, signed(9) or
        unsigned(8) might be appropriate values.

    b_shape: Shape
        The shape of each the value packed into the B input. If the B
        input is used for Conv2D filter values, signed(8) would be the
        appropriate shape.

    accumulator_shape: Shape
        The shape of the accumulator. The accumulator should have
        enough precision that it does not overflow under normal
        operation. signed(32) is usually large enough, but a narrower
        accumulator may be sufficient, and will make more efficient
        use of the FPGA fabric.

    Attributes
    ----------

    input_a: unsigned(n * a_shape.width), in
        The input data containing A values.
    output_a: unsigned(n * a_shape.width), out
        The value of input_a, delayed by one cycle.
    input_b: unsigned(n * b_shape.width), in
        The input data containing B values.
    output_b: unsigned(n * b_shape.width), out
        The value of input_b, delayed by one cycle.
    input_first: Signal(1), in
        Indicates that this is the first value passed, and the
        accumulator should be assumed zero.
    output_first: Signal(1), out
        The value of input_first, delayed by one cycle
    output_last: Signal(1), out
        The value of output_last, delayed by one cycle
    output_accumulator: accumulator_shape, out
        Holds the output value. Since there is no flow control, this
        value must be read before it is updated again.
    output_accumulator_new: Signal(1), out
        Pulsed to indicate that a new value has been placed in
        output_accumulator.
    """

    # The latency from input_received to accumulator being updated
    # Moving the accumulator to accumulator_output takes an additional
    # cycle.
    PIPELINE_CYCLES = 2

    def __init__(self, n, a_shape, b_shape, accumulator_shape):
        self._n = n
        self._a_shape = a_shape
        self._b_shape = b_shape
        self._accumulator_shape = accumulator_shape

        self.input_a = Signal(unsigned(n * a_shape.width))
        self.output_a = Signal.like(self.input_a)
        self.input_b = Signal(unsigned(n * b_shape.width))
        self.output_b = Signal.like(self.input_b)

        self.input_first = Signal()
        self.output_first = Signal()
        self.input_last = Signal()
        self.output_last = Signal()

        self.output_accumulator = Signal(accumulator_shape)
        self.output_accumulator_new = Signal()

    def _connect_passthrough(self, m):
        """Connects the block pass through signals."""
        m.d.sync += [
            self.output_a.eq(self.input_a),
            self.output_b.eq(self.input_b),
            self.output_first.eq(self.input_first),
            self.output_last.eq(self.input_last),
        ]

    def elab(self, m):
        self._connect_passthrough(m)

        # Pipeline cycle 0: calculate products
        products = []
        for i in range(self._n):
            a_bits = self.input_a.word_select(i, self._a_shape.width)
            b_bits = self.input_b.word_select(i, self._b_shape.width)
            a = Signal(self._a_shape, name=f"a_{i}")
            b = Signal(self._b_shape, name=f"b_{i}")
            m.d.comb += [
                a.eq(a_bits),
                b.eq(b_bits),
            ]
            ab = Signal.like(a * b)
            m.d.sync += ab.eq(a * b)
            products.append(ab)

        # Pipeline cycle 1: accumulate
        product_sum = Signal.like(tree_sum(products))
        m.d.comb += product_sum.eq(tree_sum(products))
        first_delayed = delay(m, self.input_first, 1)[-1]
        accumulator = Signal(self._accumulator_shape)
        base = Mux(first_delayed, 0, accumulator)
        m.d.sync += accumulator.eq(base + product_sum)

        # Pipeline cycle 2: optional accumulator output
        last_delayed = delay(m, self.input_last, 2)[-1]
        with m.If(last_delayed):
            m.d.sync += self.output_accumulator.eq(accumulator)
            m.d.sync += self.output_accumulator_new.eq(1)
        with m.Else():
            m.d.sync += self.output_accumulator_new.eq(0)
