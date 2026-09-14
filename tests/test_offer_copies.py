"""offer_copies emits a clause bound to the caller's alias and table."""
from offer_copies import not_a_copy


def test_clause_uses_the_callers_alias_and_table():
    sql = not_a_copy("co", "public.community_offers")
    assert "FROM public.community_offers e" in sql
    assert "co.created_at" in sql and "co.fare" in sql
    assert sql.strip().startswith("AND NOT EXISTS")


def test_clause_has_no_format_braces_left():
    # Callers pass it through str.format / psycopg; stray braces or % would break them.
    sql = not_a_copy("c", "app_private.offer_history")
    assert "{" not in sql and "}" not in sql and "%" not in sql
