"""build_flow_pause_frame 帧格式测试。"""

from app.domain.chat import stream as sse


class TestBuildFlowPauseFrame:
    """build_flow_pause_frame 的输出格式必须与 M6 多行协议对齐。"""

    def test_default_reason_carries_event_line_and_no_type_key(self) -> None:
        """默认 reason=backpressure 时，帧必须带 event: flow_pause 行、
        reason 字段正确、且 data 内无 type 键。"""
        frame = sse.build_flow_pause_frame()

        # event 行是前端分发依据（react-native-sse 按 event: 名分发）
        assert frame.startswith(b"event: flow_pause\n")
        # 默认分隔符 json.dumps 用 ": "（冒号后空格）
        assert b'"reason": "backpressure"' in frame
        # 类型由 event 行承载，data 不放 type
        assert b'"type"' not in frame
