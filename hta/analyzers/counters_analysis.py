# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

from typing import Dict, List, Optional

import pandas as pd

from hta.common.call_stack import CallGraph
from hta.common.trace import Trace
from hta.configs.config import logger

CUDA_SASS_INSTRUCTION_COUNTER_FLOPS: Dict[str, float] = {
    f"smsp__sass_thread_inst_executed_op_{op}_pred_on.sum": (2 if "fma" in op else 1)
    for op in ["ffma", "fmul", "fadd", "hfma", "hmul", "hadd", "dfma", "dmul", "dadd"]
}


class CountersAnalysis:
    def __init__(self):
        pass

    @classmethod
    def get_counter_data_with_operators(
        cls,
        t: "Trace",
        ranks: Optional[List[int]] = None,
        stringify: bool = True,
    ) -> List[pd.DataFrame]:
        """Correlates the Kernel counter events with pytorch operators using
        the callgraph.
        Returns a list of dataframes, one per rank
        TODO:bcoutinho improve doc
        """

        if ranks is None:
            ranks = [0]

        result_list: List[pd.DataFrame] = []
        sym_table = t.symbol_table.get_sym_table()
        sym_index = t.symbol_table.get_sym_id_map()

        if "cuda_profiler_range" not in sym_index.keys():
            logger.warning(
                "Could not find events of 'cuda_profiler_range' category "
                "Please check if you ran CUPTI profiler mode correctly"
            )
            return []

        cuda_profiler_cat = sym_index.get("cuda_profiler_range")
        cuda_runtime_cat = sym_index.get("cuda_runtime")
        cpu_op_cat = sym_index.get("cpu_op")
        # TODO:bcoutinho handle missing symbols?
        kernel_launch_sym = sym_index.get("cudaLaunchKernel")

        cg = CallGraph(t)

        for rank in ranks:
            # get trace for a rank
            trace_df: pd.DataFrame = t.get_trace(rank)

            # get cuda profiler events
            gpu_kernels = trace_df[trace_df["cat"].eq(cuda_profiler_cat)].reset_index(drop=True)

            # call stacks of interest are all on CPU
            call_stacks_idxs = cg.mapping.query(f"rank == {rank} and pid != 0 and tid != 0").csg_index.values

            # merge with runtime events from call stacks
            leaf_nodes: List[int] = []
            for i in call_stacks_idxs:
                leaf_nodes.extend(cg.call_stacks[i].get_leaf_nodes(-1))

            leaf_df = trace_df.loc[leaf_nodes]
            kernel_launches = leaf_df[leaf_df["cat"].eq(cuda_runtime_cat)]
            kernel_launches = kernel_launches[kernel_launches["name"].eq(kernel_launch_sym)].reset_index(drop=True)

            # We map 1:1 each kernel launch with gpu kernel
            # so the number of each should be equal
            # TODO:bcoutinho log this error and skip to next rank
            assert len(kernel_launches) == len(gpu_kernels)

            # Now, we do the merge using index only
            gpu_kernels = gpu_kernels.merge(
                kernel_launches[["index"]],
                left_index=True,
                right_index=True,
                how="inner",
                suffixes=[None, "_runtime"],  # adds a column index_runtime
            )

            # Add the op_stack as an array
            def get_opstack(row: pd.Series) -> List[int]:
                for i in call_stacks_idxs:
                    op_stack = cg.call_stacks[i].get_path_to_root(row.index_runtime)[:-2]
                    if len(op_stack) > 0:
                        return op_stack
                return []

            gpu_kernels["op_stack"] = gpu_kernels.apply(get_opstack, axis=1)

            def get_top_or_bottom_op(ops: List[int], top: bool) -> int:
                # only get ops that are cpu_op
                filterd_ops = [op for op in ops if trace_df.loc[op]["cat"] == cpu_op_cat]
                if len(filterd_ops) < 1:
                    return -1
                return filterd_ops[-1 if top else 0]

            gpu_kernels["top_level_op"] = gpu_kernels["op_stack"].apply(lambda ops: get_top_or_bottom_op(ops, top=True))
            gpu_kernels["bottom_level_op"] = gpu_kernels["op_stack"].apply(
                lambda ops: get_top_or_bottom_op(ops, top=False)
            )

            if stringify:
                # add back strings for readability
                for col in ["cat", "name"]:
                    gpu_kernels[col] = gpu_kernels[col].apply(
                        lambda i: sym_table[i] if (i > 0 and i < len(sym_table)) else ""
                    )
                for col in ["top_level_op", "bottom_level_op"]:
                    gpu_kernels[col] = gpu_kernels[col].apply(
                        lambda op: sym_table[trace_df.loc[op]["name"]] if op >= 0 else ""
                    )

                def stringify_op_stack(ops: List[int]) -> List[str]:
                    return [sym_table[trace_df.loc[op]["name"]] for op in ops]

                gpu_kernels["op_stack"] = gpu_kernels["op_stack"].apply(stringify_op_stack)

            result_list.append(gpu_kernels)

        return result_list
