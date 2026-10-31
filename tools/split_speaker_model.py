#!/usr/bin/env python3
"""Rebuild data/models/speaker_* from the WeSpeaker ONNX file.

    pip install onnx
    curl -LO https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/wespeaker_en_voxceleb_resnet34.onnx
    python3 tools/split_speaker_model.py wespeaker_en_voxceleb_resnet34.onnx data/models
"""
import os
import sys

import numpy as np
import onnx
from onnx import TensorProto, helper, numpy_helper

src, out = sys.argv[1], sys.argv[2]
m = onnx.load(src)
g = m.graph
init = {i.name: numpy_helper.to_array(i) for i in g.initializer}
np.savez(os.path.join(out, "speaker_head.npz"), W=init["model.seg_1.weight"],
         b=init["model.seg_1.bias"], mean=init["mean_vec"])

nodes = list(g.node)
# drop Transpose/Constant/Unsqueeze at the start: input becomes (B, 1, 80, T)
start = next(i for i, n in enumerate(nodes) if n.op_type == "Conv")
stop = next(i for i, n in enumerate(nodes) if n.name == "Relu_87")
first_conv_in = nodes[start].input[0]
trunk_out = nodes[stop].output[0]
del g.node[:]
g.node.extend(nodes[start:stop + 1])
del g.input[:]
g.input.append(helper.make_tensor_value_info(first_conv_in, TensorProto.FLOAT, ["B", 1, 80, "T"]))
del g.output[:]
g.output.append(helper.make_tensor_value_info(trunk_out, TensorProto.FLOAT, ["B", 256, 10, "Tp"]))
del g.value_info[:]
used = {i for n in g.node for i in n.input}
keep = [i for i in g.initializer if i.name in used]
del g.initializer[:]
g.initializer.extend(keep)
onnx.checker.check_model(m)
onnx.save(m, os.path.join(out, "speaker_resnet34_trunk.onnx"))
print("ok")
