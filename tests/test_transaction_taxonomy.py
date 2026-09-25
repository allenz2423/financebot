from src.services.transaction_taxonomy import (
    CATEGORY_PATHS,
    MAIN_CATEGORIES,
    TAXONOMY,
    canonicalize_taxonomy_choice,
    category_is_valid,
    category_parts,
    legacy_category_for_path,
)


def test_active_taxonomy_is_compact_and_hierarchical():
    assert len(MAIN_CATEGORIES) == 7
    assert len(CATEGORY_PATHS) == 100
    assert all(path.count(" → ") == 2 for path in CATEGORY_PATHS)


def test_uncategorized_is_not_a_model_leaf():
    assert all("Uncategorized Purchase" not in leaves for groups in TAXONOMY.values() for leaves in groups.values())


def test_old_path_is_valid_but_returns_new_canonical_parts():
    old = "Essential / Fixed Living Expenses (Needs) → Groceries & Household → Food & Groceries"
    parts = category_parts(old)
    assert category_is_valid(old)
    assert parts["main"] == "Essential / Fixed Living Expenses"
    assert parts["leaf"] == "Groceries"
    assert legacy_category_for_path(old) == "Groceries"


def test_old_transfer_path_keeps_transfer_semantics():
    old = "Transfers & Non-Expense Movements (Neutral) → Transfers → Credit Card Payments (Checking → Credit Card)"
    parts = category_parts(old)
    assert parts["is_transfer"] is True
    assert legacy_category_for_path(old) == "Credit Card Bill Payment"


def test_old_model_main_choice_is_normalized():
    assert canonicalize_taxonomy_choice("Discretionary Spending (Wants)", "main") == "Discretionary Spending"


def test_old_model_leaf_choice_is_normalized():
    assert canonicalize_taxonomy_choice(
        "Food & Groceries", "leaf", main="Essential / Fixed Living Expenses", group="Groceries & Household"
    ) == "Groceries"


def test_unknown_path_is_not_accepted():
    assert not category_is_valid("Discretionary Spending → Dining & Food → Made Up")
