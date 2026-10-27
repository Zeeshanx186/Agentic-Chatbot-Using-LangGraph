"""Integration checks: DATABASE_URL must point to the deployment database.

Uses UUID test records and removes only those records afterwards.
Run: python -m unittest tests.test_cloud_deployment -v
"""

import os
from pathlib import Path
import unittest
import uuid

import dexter.cloud_store as cloud_store


@unittest.skipUnless(cloud_store.ENABLED, "DATABASE_URL is required")
class CloudDeploymentTest(unittest.TestCase):
    def test_persistence_and_authenticated_file_handoff(self):
        from fastapi.testclient import TestClient
        from langchain_core.messages import HumanMessage
        from langgraph.graph import StateGraph, MessagesState, START, END
        import dexter.chat_store as chat_store

        os.environ.setdefault("DEXTER_STAGING_PASSWORD", uuid.uuid4().hex)
        import dexter.vercel_app as vercel_app

        thread = "deployment-test-" + uuid.uuid4().hex
        file_id = None
        path = cloud_store.DATA_ROOT / "uploads" / (thread + ".txt")
        try:
            chat_store.save_title(thread, "Neon roundtrip")
            chat_store.save_feedback(thread, 1, 1)
            chat_store.save_feedback(thread, 2, 0)
            self.assertEqual(chat_store.load_titles()[thread], "Neon roundtrip")
            self.assertEqual(chat_store.load_feedback()[thread], {"1": 1, "2": 0})

            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"survives another instance")
            cloud_store.persist_path(path)
            path.unlink()
            cloud_store.restore_path(path)
            self.assertEqual(path.read_bytes(), b"survives another instance")

            builder = StateGraph(MessagesState)
            builder.add_node("echo", lambda state: {"messages": []})
            builder.add_edge(START, "echo")
            builder.add_edge("echo", END)
            graph = builder.compile(checkpointer=cloud_store.checkpoint())
            config = {"configurable": {"thread_id": thread}}
            graph.invoke({"messages": [HumanMessage(content="persist me")]}, config)
            from langgraph.checkpoint.postgres import PostgresSaver
            fresh = builder.compile(checkpointer=PostgresSaver(cloud_store.pool()))
            self.assertEqual(fresh.get_state(config).values["messages"][0].content, "persist me")

            client = TestClient(vercel_app.app)
            self.assertEqual(client.get("/healthz").status_code, 200)
            session = str(uuid.uuid4())
            response = client.post("/project/file", params={"session_id": session},
                                   files={"file": ("test.csv", b"x,y\n1,2", "text/csv")})
            self.assertIn(response.status_code, (401, 403))
            login = client.post("/login", data={"username": "dexter",
                                               "password": os.environ["DEXTER_STAGING_PASSWORD"]})
            self.assertEqual(login.status_code, 200, login.text)
            response = client.post("/project/file", params={"session_id": session},
                                   files={"file": ("test.csv", b"x,y\n1,2", "text/csv")})
            self.assertEqual(response.status_code, 200, response.text)
            file_id = response.json()["id"]
            # No WebsocketSession exists in this HTTP worker.
            response = client.get("/project/file/" + file_id, params={"session_id": session})
            self.assertEqual(response.content, b"x,y\n1,2")
            self.assertEqual(client.get("/project/file/" + file_id,
                params={"session_id": str(uuid.uuid4())}).status_code, 404)
            self.assertEqual(client.post("/project/file", params={"session_id": session},
                files={"file": ("big.csv", b"x" * (4 * 1024 * 1024 + 1), "text/csv")}).status_code, 413)
        finally:
            cloud_store.checkpoint().delete_thread(thread)
            chat_store.delete_title(thread)
            for index in (1, 2):
                chat_store.save_feedback(thread, index, None)
            with cloud_store.pool().connection() as conn:
                conn.execute("DELETE FROM dexter_objects WHERE key=%s",
                             ("file/uploads/" + path.name,))
                if file_id:
                    conn.execute("DELETE FROM dexter_objects WHERE key=%s", ("transfer/" + file_id,))
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
