"""保持 Pydantic 1 / 2 的模拟请求序列化结果一致。"""

from pydantic import BaseModel


def model_to_dict(model: BaseModel) -> dict:
    if hasattr(model, "model_dump"):
        return model.model_dump()
    return model.dict()
