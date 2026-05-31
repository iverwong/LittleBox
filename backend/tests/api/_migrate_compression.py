"""Batch migrate remaining compression tests in test_chat_stream_graph.py."""
PATH = "tests/api/test_chat_stream_graph.py"

with open(PATH, encoding="utf-8") as f:
    content = f.read()

# We'll do targeted replacements for each test
# Strategy: replace unique signatures/blocks

replacements = [
    # (old, new) pairs

    # 1. test_compression_row84_regression - decorator + first line
    (
        '@pytest.mark.asyncio\nasync def test_compression_row84_regression(\n    app_with_eval, api_client_with_eval, auth_headers_child, db_session, compression_session, monkeypatch,\n):',
        '@pytest.mark.asyncio\nasync def test_compression_row84_regression(lifecycle_ctx):',
    ),
    # 2. test_compression_messages_order_assertion
    (
        '@pytest.mark.asyncio\nasync def test_compression_messages_order_assertion(\n    app_with_eval, api_client_with_eval, auth_headers_child, db_session, compression_session, monkeypatch,\n):',
        '@pytest.mark.asyncio\nasync def test_compression_messages_order_assertion(lifecycle_ctx):',
    ),
    # 3. test_compression_noop_empty_filter
    (
        '@pytest.mark.asyncio\nasync def test_compression_noop_empty_filter(\n    app_with_eval, api_client_with_eval, auth_headers_child, db_session, child_user, monkeypatch,\n):',
        '@pytest.mark.asyncio\nasync def test_compression_noop_empty_filter(lifecycle_ctx):',
    ),
    # 4. test_compression_with_existing_summary
    (
        '@pytest.mark.asyncio\nasync def test_compression_with_existing_summary(\n    app_with_eval, api_client_with_eval, auth_headers_child, db_session, child_user, monkeypatch,\n):',
        '@pytest.mark.asyncio\nasync def test_compression_with_existing_summary(lifecycle_ctx):',
    ),
]

total = 0
for old, new in replacements:
    c = content.count(old)
    if c > 0:
        content = content.replace(old, new)
        total += c
        print(f"Replaced signature ({c})")
    else:
        print("NOT FOUND")

# Now replace remaining fixture references within compression test bodies
# These are UNIQUE strings within the compression tests section
body_replacements = [
    # app_with_eval.state.resources -> lifecycle_ctx.rr
    ('app_with_eval.state.resources.main_graph.astream = fake_astream\n\n    with patch("app.chat.factory.build_provider_llm", return_value=fake_c_llm):\n        body = make_payload(content="继续聊聊", session_id=str(sid))\n        resp = await api_client_with_eval.post(',
     'lifecycle_ctx.rr.main_graph.astream = fake_astream\n\n    with patch("app.chat.factory.build_provider_llm", return_value=fake_c_llm):\n        body = make_payload(content="继续聊聊", session_id=str(sid))\n        resp = await client.post('),
    # row84 regression - app_with_eval + api_client_with_eval
    ('app_with_eval.state.resources.main_graph.astream = _fake_astream_84\n\n    with patch("app.chat.factory.build_provider_llm", return_value=fake_c_llm):\n        body = make_payload(content="继续聊聊", session_id=str(sid))\n        resp = await api_client_with_eval.post(',
     'lifecycle_ctx.rr.main_graph.astream = _fake_astream_84\n\n    with patch("app.chat.factory.build_provider_llm", return_value=fake_c_llm):\n        body = make_payload(content="继续聊聊", session_id=str(sid))\n        resp = await client.post('),
    # messages_order - app_with_eval + api_client_with_eval
    ('app_with_eval.state.resources.main_graph.astream = spy_astream\n\n    with _patch("app.chat.factory.build_provider_llm", return_value=fake_c_llm):\n        body = make_payload(content="继续聊聊", session_id=str(sid))\n        resp = await api_client_with_eval.post(',
     'lifecycle_ctx.rr.main_graph.astream = spy_astream\n\n    with _patch("app.chat.factory.build_provider_llm", return_value=fake_c_llm):\n        body = make_payload(content="继续聊聊", session_id=str(sid))\n        resp = await client.post('),
    # app_with_eval for noop test
    ('app_with_eval.state.resources.main_graph.astream = fake_astream\n\n    body = make_payload(content="新的消息", session_id=str(sid))\n    resp = await api_client_with_eval.post(',
     'lifecycle_ctx.rr.main_graph.astream = fake_astream\n\n    body = make_payload(content="新的消息", session_id=str(sid))\n    resp = await client.post('),
    # db_session -> lifecycle_ctx.assert_sess in compression tests (with expire_all)
    ('await db_session.execute(\n            select(Message).where(\n                Message.session_id == sid, Message.role == MessageRole.summary,\n            )\n        )',
     "lifecycle_ctx.assert_sess.expire_all()\n    summary = (\n        await lifecycle_ctx.assert_sess.execute(\n            select(Message).where(\n                Message.session_id == sid, Message.role == MessageRole.summary,\n            )\n        )"),
    ('await db_session.get(Message, mid)',
     'await lifecycle_ctx.assert_sess.get(Message, mid)'),
    # headers, child = auth_headers_child -> lifecycle_setup
    ('headers, child = auth_headers_child\n    sid, _, msg1_id, msg2_id = compression_session',
     'client, headers, child = await lifecycle_setup(lifecycle_ctx)\n    sid, _, msg1_id, msg2_id = await seed_compression_session(lifecycle_ctx, child)'),
    ('_result2 = await db_session.execute(',
     '_result2 = await lifecycle_ctx.assert_sess.execute('),
    ('session_row = (\n        await db_session.execute(',
     'session_row = (\n        await lifecycle_ctx.assert_sess.execute('),
]

for old, new in body_replacements:
    c = content.count(old)
    if c > 0:
        content = content.replace(old, new)
        total += c
        print(f"Replaced body ({c})")
    else:
        print(f"NOT FOUND in body: {old[:50]}...")

with open(PATH, "w", encoding="utf-8") as f:
    f.write(content)
print(f"\nTotal replacements: {total}")
