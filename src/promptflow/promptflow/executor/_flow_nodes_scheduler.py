# ---------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# ---------------------------------------------------------

import contextvars
from concurrent import futures
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Dict, List, Tuple

from promptflow._core.flow_execution_context import FlowExecutionContext
from promptflow._core.tools_manager import ToolsManager
from promptflow._utils.logger_utils import logger
from promptflow._utils.utils import set_context
from promptflow.contracts.flow import Node
from promptflow.executor._dag_manager import DAGManager
from promptflow.executor._errors import NoNodeExecutedError

RUN_FLOW_NODES_LINEARLY = 1
DEFAULT_CONCURRENCY_BULK = 2
DEFAULT_CONCURRENCY_FLOW = 16


class FlowNodesScheduler:
    def __init__(self, tools_manager: ToolsManager) -> None:
        self.tools_manager = tools_manager

    def execute(
        self, context: FlowExecutionContext, inputs: Dict, nodes_from_invoker: List[Node], node_concurrency: int
    ) -> Tuple[dict, dict]:
        dag_manager = DAGManager(nodes_from_invoker, inputs)
        node_concurrency = min(node_concurrency, DEFAULT_CONCURRENCY_FLOW)
        logger.info(f"Start to run {len(nodes_from_invoker)} nodes with concurrency level {node_concurrency}.")

        self.context = context
        self.inputs = inputs
        parent_context = contextvars.copy_context()
        with ThreadPoolExecutor(
            max_workers=node_concurrency, initializer=set_context, initargs=(parent_context,)
        ) as executor:
            self.future_to_node: Dict[Future, Node] = {}
            self._execute_nodes(dag_manager, executor)

            while not dag_manager.completed():
                try:
                    if not self.future_to_node:
                        raise NoNodeExecutedError("No nodes are ready for execution, but the flow is not completed.")
                    completed_futures, _ = futures.wait(self.future_to_node.keys(), return_when=futures.FIRST_COMPLETED)
                    dag_manager.complete_nodes(self._collect_outputs(completed_futures))
                    for each_future in completed_futures:
                        del self.future_to_node[each_future]
                    self._execute_nodes(dag_manager, executor)
                except Exception as e:
                    for unfinished_future in self.future_to_node.keys():
                        node_name = self.future_to_node[unfinished_future].name
                        logger.error(f"One node execution failed, cancel all running tasks. {node_name}.")
                        # We can't cancel running tasks here, only pending tasks could be cancelled.
                        unfinished_future.cancel()
                    # Even we raise exception here, still need to wait all running jobs finish to exit.
                    raise e
        return dag_manager.completed_nodes_outputs, dag_manager.bypassed_nodes

    def _execute_nodes(self, dag_manager: DAGManager, executor: ThreadPoolExecutor):
        # Skip nodes and update node run info until there are no nodes to bypass
        nodes_to_bypass = dag_manager.pop_bypassable_nodes()
        while nodes_to_bypass:
            self._bypass_nodes(dag_manager, nodes_to_bypass)
            nodes_to_bypass = dag_manager.pop_bypassable_nodes()

        # Submit nodes that are ready to run
        nodes_to_exec = dag_manager.pop_ready_nodes()
        if nodes_to_exec:
            self._submit_nodes(executor, dag_manager, nodes_to_exec)

    def _collect_outputs(self, completed_futures: List[Future]):
        completed_nodes_outputs = {}
        for each_future in completed_futures:
            each_node_result = each_future.result()
            each_node = self.future_to_node[each_future]
            completed_nodes_outputs[each_node.name] = each_node_result
        return completed_nodes_outputs

    def _bypass_nodes(self, dag_manager: DAGManager, nodes: List[Node]):
        try:
            self.context.start()
            for node in nodes:
                node_outputs = dag_manager.get_bypassed_node_outputs(node)
                self.context.bypass_node(node, node_outputs)
        finally:
            self.context.end()

    def _submit_nodes(self, executor: ThreadPoolExecutor, dag_manager: DAGManager, nodes):
        for each_node in nodes:
            future = executor.submit(self._exec_single_node_in_thread, (each_node, dag_manager))
            self.future_to_node[future] = each_node

    def _exec_single_node_in_thread(self, args: Tuple[Node, DAGManager]):
        node, dag_manager = args
        # We are using same run tracker and cache manager for all threads, which may not thread safe.
        # But for bulk run scenario, we've doing this for a long time, and it works well.
        context = self.context.copy()
        try:
            context.start()
            kwargs = dag_manager.get_node_valid_inputs(node)
            f = self.tools_manager.get_tool(node.name)
            context.current_node = node
            result = f(**kwargs)
            context.current_node = None
            return result
        finally:
            context.end()
