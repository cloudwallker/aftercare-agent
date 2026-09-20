"""任务查询游标和事件接口只接受约定数据。"""

import base64
import json

import pytest


@pytest.mark.parametrize(
    "values",
    [["2026-09-20T00:00:00+00:00", {}], ["2026-09-20T00:00:00+00:00", None], [1, 2], [True, 1]],
)
def test_malformed_cursor_is_a_validation_error(values):
    from aftercare.commands import DomainError
    from aftercare.tasks import decode_cursor

    cursor = base64.urlsafe_b64encode(json.dumps(values).encode()).decode()
    with pytest.raises(DomainError) as error:
        decode_cursor(cursor)
    assert error.value.status == 422
