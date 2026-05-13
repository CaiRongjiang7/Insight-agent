from app.repository import UserRepository


def get_user_summary(user_id: int) -> dict[str, object]:
    repository = UserRepository()
    record = repository.get_user(user_id)
    return {"userName": record["name"], "id": record["id"]}
