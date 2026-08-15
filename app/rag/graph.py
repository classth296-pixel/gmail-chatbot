# ================================================
# app/rag/graph.py
# Compiles the LangGraph StateGraph:
#   START -> contextualize -> retrieve -> grade -> generate -> END
# Checkpointed per-user with SqliteSaver so multi-turn state (including
# structured filter follow-ups) survives across requests/restarts.
# ================================================

import sqlite3

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.sqlite import SqliteSaver

from app.rag.nodes import (
    GraphState, make_contextualize_node, make_retrieve_node,
    make_grade_node, make_generate_node, build_trimmer,
)


def compile_graph(model, get_bundle, checkpoint_db_path: str):
    graph = StateGraph(GraphState)

    graph.add_node("contextualize", make_contextualize_node(model))
    graph.add_node("retrieve", make_retrieve_node(get_bundle))
    graph.add_node("grade", make_grade_node(model))
    graph.add_node("generate", make_generate_node(model, build_trimmer()))

    graph.add_edge(START, "contextualize")
    graph.add_edge("contextualize", "retrieve")
    graph.add_edge("retrieve", "grade")
    graph.add_edge("grade", "generate")
    graph.add_edge("generate", END)

    conn = sqlite3.connect(checkpoint_db_path, check_same_thread=False)
    checkpointer = SqliteSaver(conn)

    return graph.compile(checkpointer=checkpointer)
