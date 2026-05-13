class UserRepository:
    def get_user(self, user_id: int) -> dict[str, object]:
        return {"id": user_id, "name": "Ada"}
