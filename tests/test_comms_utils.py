import pytest

from rlm.core.comms_utils import LMRequest


def test_lmrequest_from_dict_accepts_valid_depth():
    request = LMRequest.from_dict(
        {
            "prompt": "hello",
            "depth": 2,
        }
    )

    assert request.prompt == "hello"
    assert request.depth == 2


def test_lmrequest_from_dict_rejects_missing_depth():
    with pytest.raises(ValueError, match="depth"):
        LMRequest.from_dict(
            {
                "prompt": "hello",
            }
        )
