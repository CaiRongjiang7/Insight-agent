"""Small-team migration command used by Docker Compose."""

import os

from .retrospective_store import PostgresRetrospectiveStore


def main() -> None:
    database_url = os.environ["DATABASE_URL"]
    store = PostgresRetrospectiveStore(database_url)
    store.initialize()
    print(f"retrospective schema ready via {type(store).__name__}")


if __name__ == "__main__":
    main()
