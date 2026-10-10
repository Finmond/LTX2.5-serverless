import asyncio
import os
import unittest
from unittest.mock import AsyncMock, Mock, patch

import comfy_deadline
from tests import test_handler_health as fixtures


class BoundedHandlerTests(unittest.TestCase):
    load_handler = fixtures.TestHandlerHealth.load_handler
    def test_guard_is_armed_before_workflow_and_disarmed_only_on_success(self):
        module, _ = self.load_handler()
        module.redis_client = AsyncMock()
        lease = {"job": "test-only"}
        with patch.dict(os.environ, {"FINMOND_BOUNDED_RENDER": "1"}), \
                patch.object(comfy_deadline, "arm", return_value=lease) as arm, \
                patch.object(comfy_deadline, "finish") as finish, \
                patch.object(module, "handle_workflow_job", new=AsyncMock(return_value={"status": "success"})) as render:
            result = asyncio.run(module.handler({"id": "one-job", "input": {
                "workflow": {"1": {"class_type": "Fixture", "inputs": {}}},
                "render_timeout_seconds": 600}}))
            self.assertEqual(result["status"], "success")
            arm.assert_called_once_with("one-job", 600)
            render.assert_awaited_once()
            finish.assert_called_once_with(lease, success=True)

    def test_invalid_or_missing_guard_prevents_inference(self):
        module, _ = self.load_handler()
        module.redis_client = AsyncMock()
        with patch.dict(os.environ, {"FINMOND_BOUNDED_RENDER": "1"}), \
                patch.object(comfy_deadline, "arm", side_effect=comfy_deadline.GuardError("Invalid render deadline")), \
                patch.object(module, "handle_workflow_job", new=AsyncMock()) as render:
            result = asyncio.run(module.handler({"id": "one-job", "input": {
                "workflow": {"1": {"class_type": "Fixture", "inputs": {}}}}}))
            self.assertEqual(result["status"], "error")
            render.assert_not_awaited()

    def test_inference_failure_requests_renderer_shutdown(self):
        module, _ = self.load_handler()
        module.redis_client = AsyncMock()
        lease = {"job": "test-only"}
        with patch.dict(os.environ, {"FINMOND_BOUNDED_RENDER": "1"}), \
                patch.object(comfy_deadline, "arm", return_value=lease), \
                patch.object(comfy_deadline, "finish") as finish, \
                patch.object(module, "handle_workflow_job", new=AsyncMock(side_effect=RuntimeError("Fixture failed"))):
            result = asyncio.run(module.handler({"id": "one-job", "input": {
                "workflow": {"1": {"class_type": "Fixture", "inputs": {}}},
                "render_timeout_seconds": 600}}))
            self.assertEqual(result["status"], "error")
            finish.assert_called_once_with(lease, success=False)

    def test_bounded_workflow_never_fails_over_to_a_second_render(self):
        module, _ = self.load_handler()
        module.redis_client = AsyncMock()
        session = Mock()
        session.post.side_effect = RuntimeError("Fixture submission failure")
        with patch.dict(os.environ, {"FINMOND_BOUNDED_RENDER": "1"}), \
                patch.object(module.GPUFleetManager, "get_best_node", new=AsyncMock(return_value="127.0.0.1:8188")) as choose:
            with self.assertRaises(RuntimeError):
                asyncio.run(module.execute_workflow_with_failover(session, {}, "one-job", False, 0))
            choose.assert_awaited_once()
            session.post.assert_called_once()
