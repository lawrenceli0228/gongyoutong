"""GYT 后端测试包。

统一规则:全部 mock,绝不真调外部 API;异步测试靠 pyproject 里的
``asyncio_mode="auto"``,不用手动加 ``@pytest.mark.asyncio``。
公共 fixture 见 ``tests/conftest.py``。
"""
