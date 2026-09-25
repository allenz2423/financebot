"""Canonical, compact hierarchical transaction taxonomy.

The active tree intentionally collapses leaves that cannot be distinguished
reliably from bank descriptors. Historical paths and legacy UI labels remain
valid through compatibility aliases; model-facing choices use only this tree.
"""

from __future__ import annotations


TAXONOMY: dict[str, dict[str, tuple[str, ...]]] = {
    "Income & Inflows": {
        "Employment & Contract Income": ("Salary / Wages", "Bonus / Commission", "Tips", "Freelance / Gig Income", "Business Revenue"),
        "Investment & Property Income": ("Dividends", "Interest Income", "Capital Gains", "Rental / Royalty Income"),
        "Other Inflows": ("Government Benefits", "Gifts Received", "Refunds / Reimbursements"),
    },
    "Essential / Fixed Living Expenses": {
        "Housing": ("Rent", "Mortgage Payment", "Property Tax / HOA", "Housing Insurance", "Housing Deposit / Moving"),
        "Utilities & Communications": ("Electricity / Gas", "Water / Waste", "Internet / Phone"),
        "Groceries & Household": ("Groceries", "Household Supplies", "Baby / Family Necessities"),
        "Transportation": ("Fuel / EV Charging", "Public Transit", "Vehicle Ownership / Loan", "Vehicle Maintenance", "Parking / Tolls"),
        "Healthcare": ("Health Insurance", "Medical / Dental / Vision Care", "Pharmacy / Medical Supplies"),
        "Dependents & Family": ("Childcare", "Support / Alimony", "Elder Care"),
    },
    "Discretionary Spending": {
        "Dining & Food": ("Restaurant / Takeout", "Coffee / Bakery", "Alcohol / Nightlife", "Delivery Fees"),
        "Shopping": ("Clothing / Apparel", "Electronics", "Home Goods / Furniture", "Sporting Goods / Hobbies", "General Retail"),
        "Entertainment & Media": ("Movies / Live Events", "Gaming", "Books / Publications"),
        "Digital Subscriptions & Services": ("Video Streaming", "Music Streaming", "Software / Cloud Services", "News / Editorial", "Digital Memberships"),
        "Personal Care & Wellness": ("Hair / Beauty", "Fitness", "Spa / Wellness"),
        "Travel": ("Airfare", "Lodging", "Ground Transportation", "Activities", "Travel Services"),
        "Pets": ("Pet Food / Supplies", "Veterinary / Pet Medical", "Pet Services"),
        "Gifts & Charity": ("Gifts / Celebrations", "Charity / Tipping"),
    },
    "Maintenance, Education & Business": {
        "Home Maintenance & Improvement": ("Repairs", "Landscaping / Outdoor", "Hardware / Appliances"),
        "Education": ("Tuition / Fees", "Academic Materials", "Professional Development"),
        "Business": ("Office / Operations", "Business Software / Hosting", "Professional Services", "Marketing", "Business Travel / Meals"),
        "Life Events": ("Life Event Expense",),
    },
    "Financial Management & Wealth": {
        "Financial Fees": ("Bank Fees", "Credit Card Fees", "ATM / FX Fees", "Investment Fees"),
        "Taxes": ("Income / Estimated Taxes", "Investment / Capital Gains Taxes", "Property / Business Taxes"),
        "Debt": ("Credit Card Interest", "Loan Payment"),
        "Savings & Retirement": ("Savings Contribution / Withdrawal", "Retirement Contribution / Withdrawal"),
        "Investments": ("Brokerage Securities", "Crypto / Alternative Assets"),
    },
    "Transfers & Non-Expense Movements": {
        "Transfers": ("Internal Account Transfer", "Credit Card Payment", "P2P Transfer", "Investment Cash Movement", "Loan Movement", "Reimbursement / Shared Expense"),
    },
    "Adjustments & Unresolved": {
        "Adjustments": ("Cash Withdrawal / Deposit", "Refund / Merchant Credit", "Chargeback / Reversal", "Bank / Merchant Correction", "Uncategorized / Abstention"),
    },
}

UNCATEGORIZED_PATH = "Uncategorized Purchase"
MAIN_CATEGORIES: tuple[str, ...] = tuple(TAXONOMY)


def _path(main: str, group: str, leaf: str) -> str:
    return f"{main} → {group} → {leaf}"


CATEGORY_PATHS: tuple[str, ...] = tuple(
    _path(main, group, leaf)
    for main, groups in TAXONOMY.items()
    for group, leaves in groups.items()
    for leaf in leaves
)
TRANSACTION_CATEGORIES: tuple[str, ...] = CATEGORY_PATHS + (UNCATEGORIZED_PATH,)
CATEGORY_DESCRIPTIONS = {
    _path(main, group, leaf): f"{leaf}; main area: {main}; subgroup: {group}."
    for main, groups in TAXONOMY.items()
    for group, leaves in groups.items()
    for leaf in leaves
}
CATEGORY_DESCRIPTIONS[UNCATEGORIZED_PATH] = "Insufficient evidence to safely choose a hierarchical category."

LEGACY_TRANSACTION_CATEGORIES: tuple[str, ...] = (
    "Dining Out", "Groceries", "Shopping", "Entertainment", "Utilities",
    "Transportation / Gas", "Healthcare / Medical", "Income / Paycheck",
    "Credit Card Bill Payment", "P2P Transfer", "Travel / Vacation",
    "Subscription", "Business / Tax Deductible", UNCATEGORIZED_PATH,
)


def _old_path(main: str, group: str, leaf: str) -> str:
    return _path(main, group, leaf)


def _build_historical_aliases() -> dict[str, str]:
    """Translate the former active taxonomy into the compact tree."""
    aliases: dict[str, str] = {}
    old_to_new_main = {
        "Essential / Fixed Living Expenses (Needs)": "Essential / Fixed Living Expenses",
        "Discretionary Spending (Wants)": "Discretionary Spending",
        "Transfers & Non-Expense Movements (Neutral)": "Transfers & Non-Expense Movements",
    }

    def add(main: str, group: str, leaf: str, new_group: str, new_leaf: str, new_main: str | None = None) -> None:
        aliases[_old_path(main, group, leaf)] = _path(new_main or old_to_new_main.get(main, main), new_group, new_leaf)

    for leaf in ("Salary / Wages", "Bonus", "Tips", "Commission", "Self-Employment / Freelance", "Business Revenue"):
        add("Income & Inflows", "Earned Income", leaf, "Employment & Contract Income", {"Bonus": "Bonus / Commission", "Commission": "Bonus / Commission", "Self-Employment / Freelance": "Freelance / Gig Income"}.get(leaf, leaf))
    for leaf in ("Capital Gains", "Dividends", "Interest Earned", "Rental Property Income", "Royalties"):
        add("Income & Inflows", "Passive & Investment Income", leaf, "Investment & Property Income", {"Interest Earned": "Interest Income", "Rental Property Income": "Rental / Royalty Income", "Royalties": "Rental / Royalty Income"}.get(leaf, leaf))
    for leaf in ("Tax Refunds", "Gifts Received", "Refunds / Reimbursements", "Government Benefits / Support"):
        add("Income & Inflows", "Other Inflows", leaf, "Other Inflows", {"Tax Refunds": "Refunds / Reimbursements", "Government Benefits / Support": "Government Benefits"}.get(leaf, leaf))

    essential = {
        "Housing & Shelter": ("Housing", {"Rent": "Rent", "Mortgage Principal & Interest": "Mortgage Payment", "Property Taxes": "Property Tax / HOA", "HOA / Condo Fees": "Property Tax / HOA", "Homeowners / Renters Insurance": "Housing Insurance"}),
        "Utilities & Bills": ("Utilities & Communications", {"Electricity": "Electricity / Gas", "Natural Gas / Heating Oil": "Electricity / Gas", "Water / Sewer / Trash": "Water / Waste", "Internet / Broadband": "Internet / Phone", "Mobile Phone / Cellular": "Internet / Phone"}),
        "Groceries & Household": ("Groceries & Household", {"Food & Groceries": "Groceries", "Household Supplies (Toiletries, Cleaning Products, Paper Goods)": "Household Supplies"}),
        "Transportation": ("Transportation", {"Fuel / Gas": "Fuel / EV Charging", "Public Transit / Commuter Passes": "Public Transit", "Auto Insurance": "Vehicle Ownership / Loan", "Vehicle Maintenance & Repairs": "Vehicle Maintenance", "Parking Fees & Tolls": "Parking / Tolls", "Electric Vehicle Charging": "Fuel / EV Charging"}),
        "Healthcare & Medical": ("Healthcare", {"Health Insurance Premiums": "Health Insurance", "Doctor & Specialist Visits": "Medical / Dental / Vision Care", "Dental Care": "Medical / Dental / Vision Care", "Vision Care (Glasses, Contacts, Exams)": "Medical / Dental / Vision Care", "Pharmacy & Prescription Medications": "Pharmacy / Medical Supplies", "Medical Supplies & Devices": "Pharmacy / Medical Supplies"}),
        "Debt Repayment (Non-Mortgage)": ("Debt", {"Student Loans": "Loan Payment", "Auto Loans": "Loan Payment", "Personal Loans": "Loan Payment", "Credit Card Interest / Charges": "Credit Card Interest"}),
        "Dependents & Family": ("Dependents & Family", {"Childcare / Daycare / Babysitting": "Childcare", "Child Support / Alimony": "Support / Alimony", "Elder Care": "Elder Care"}),
    }
    for group, (new_group, leaves) in essential.items():
        for leaf, new_leaf in leaves.items():
            add("Essential / Fixed Living Expenses (Needs)", group, leaf, new_group, new_leaf, "Financial Management & Wealth" if group == "Debt Repayment (Non-Mortgage)" else None)

    discretionary = {
        "Dining & Food Delivery": ("Dining & Food", {"Restaurants & Casual Dining": "Restaurant / Takeout", "Fast Food & Takeout": "Restaurant / Takeout", "Coffee Shops & Bakeries": "Coffee / Bakery", "Bars, Alcohol & Nightlife": "Alcohol / Nightlife", "Food Delivery Services & Fees": "Delivery Fees"}),
        "Shopping & Apparel": ("Shopping", {"Clothing & Footwear": "Clothing / Apparel", "Electronics, Gadgets & Hardware": "Electronics", "Furniture & Home Decor": "Home Goods / Furniture", "Sporting Goods & Outdoor Gear": "Sporting Goods / Hobbies"}),
        "Entertainment & Leisure": ("Entertainment & Media", {"Concerts, Movies & Live Events": "Movies / Live Events", "Hobbies, Craft Supplies & Equipment": "Sporting Goods / Hobbies", "Gaming (Video Games, Consoles, In-Game Purchases)": "Gaming", "Books, Magazines & Audiobooks": "Books / Publications"}),
        "Subscriptions & Digital Services": ("Digital Subscriptions & Services", {"Video Streaming (Netflix, Hulu, etc.)": "Video Streaming", "Music Streaming (Spotify, Apple Music)": "Music Streaming", "Software, Cloud Storage & SaaS Apps": "Software / Cloud Services", "News & Editorial Subscriptions": "News / Editorial"}),
        "Personal Care & Wellness": ("Personal Care & Wellness", {"Haircuts, Salons & Barbers": "Hair / Beauty", "Cosmetics & Skincare": "Hair / Beauty", "Gym Memberships & Fitness Classes": "Fitness", "Spa, Massage & Personal Wellness": "Spa / Wellness"}),
        "Travel & Vacation": ("Travel", {"Airfare": "Airfare", "Hotels & Short-Term Rentals": "Lodging", "Rental Cars & Rideshares (Uber, Lyft)": "Ground Transportation", "Vacation Dining & Activities": "Activities", "Travel Insurance": "Travel Services"}),
        "Pets": ("Pets", {"Pet Food & Treats": "Pet Food / Supplies", "Veterinary Care & Medication": "Veterinary / Pet Medical", "Pet Supplies & Toys": "Pet Food / Supplies", "Grooming, Boarding & Dog Walking": "Pet Services"}),
        "Gifts & Celebrations": ("Gifts & Charity", {"Holiday & Birthday Gifts": "Gifts / Celebrations", "Special Occasions & Weddings": "Gifts / Celebrations", "Charitable Donations & Tipping": "Charity / Tipping"}),
    }
    for group, (new_group, leaves) in discretionary.items():
        for leaf, new_leaf in leaves.items():
            add("Discretionary Spending (Wants)", group, leaf, new_group, new_leaf)

    maintenance = {
        "Home Maintenance & Improvement": ("Home Maintenance & Improvement", {"Lawn Care, Landscaping & Snow Removal": "Landscaping / Outdoor", "Home Repairs, Plumbing & Electrical": "Repairs", "Tools, Hardware & Appliances": "Hardware / Appliances"}),
        "Education & Professional Growth": ("Education", {"Tuition & School Fees": "Tuition / Fees", "Textbooks, Software & Course Materials": "Academic Materials", "Professional Certifications, Licenses & Exam Fees": "Professional Development", "Seminars, Workshops & Books": "Professional Development"}),
        "Business & Tax-Deductible Expenses": ("Business", {"Business Travel & Meals": "Business Travel / Meals", "Home Office Supplies & Equipment": "Office / Operations", "Professional Services (Legal, Accounting, CPA)": "Professional Services", "Advertising, Marketing & Web Hosting": "Marketing"}),
    }
    for group, (new_group, leaves) in maintenance.items():
        for leaf, new_leaf in leaves.items():
            add("Maintenance, Education & Life Events", group, leaf, new_group, new_leaf)

    financial = {
        "Fees & Financial Charges": ("Financial Fees", {"Bank Monthly Fees & Overdraft Charges": "Bank Fees", "Credit Card Annual Fees": "Credit Card Fees", "ATM & FX / Foreign Transaction Fees": "ATM / FX Fees", "Investment Management / Advisory Fees": "Investment Fees"}),
        "Taxes (Non-Withheld)": ("Taxes", {"Estimated Income Taxes (Federal, State, Local)": "Income / Estimated Taxes", "Capital Gains Taxes": "Investment / Capital Gains Taxes"}),
        "Savings & Wealth Building": ("Savings & Retirement", {"Emergency Fund Contributions": "Savings Contribution / Withdrawal", "High-Yield Savings Account (HYSA) Transfers": "Savings Contribution / Withdrawal", "Retirement Contributions (401k, IRA, Roth IRA)": "Retirement Contribution / Withdrawal", "Taxable Brokerage / Stock Investments": "Brokerage Securities", "Crypto / Alternative Assets": "Crypto / Alternative Assets"}),
    }
    for group, (new_group, leaves) in financial.items():
        for leaf, new_leaf in leaves.items():
            add("Financial Management, Fees & Savings", group, leaf, "Investments" if new_leaf in {"Brokerage Securities", "Crypto / Alternative Assets"} else new_group, new_leaf)

    transfers = {
        "Credit Card Payments (Checking → Credit Card)": "Credit Card Payment",
        "Internal Account Transfers (Checking ↔ Savings)": "Internal Account Transfer",
        "P2P Transfers (Venmo, Zelle, Cash App)": "P2P Transfer",
        "Investment Cash Movements (Checking → Brokerage Cash Settlement)": "Investment Cash Movement",
        "Reimbursements & Splitting Bills": "Reimbursement / Shared Expense",
    }
    for leaf, new_leaf in transfers.items():
        add("Transfers & Non-Expense Movements (Neutral)", "Transfers", leaf, "Transfers", new_leaf)
    return aliases


HISTORICAL_CATEGORY_ALIASES = _build_historical_aliases()


def canonicalize_category_path(category: str | None) -> str:
    value = str(category or "").strip()
    return HISTORICAL_CATEGORY_ALIASES.get(value, value)


def canonicalize_taxonomy_choice(value: str | None, level: str, *, main: str | None = None, group: str | None = None) -> str:
    """Normalize a model's old main/group/leaf choice to the active tree."""
    raw = str(value or "").strip()
    if level == "main":
        return {
            "Essential / Fixed Living Expenses (Needs)": "Essential / Fixed Living Expenses",
            "Discretionary Spending (Wants)": "Discretionary Spending",
            "Transfers & Non-Expense Movements (Neutral)": "Transfers & Non-Expense Movements",
        }.get(raw, raw)
    active_main = canonicalize_taxonomy_choice(main, "main")
    candidates: set[str] = set()
    for old_path, new_path in HISTORICAL_CATEGORY_ALIASES.items():
        old_parts = old_path.split(" → ")
        new_parts = new_path.split(" → ")
        if len(old_parts) != 3 or len(new_parts) != 3 or new_parts[0] != active_main:
            continue
        if level == "group" and old_parts[1] == raw:
            candidates.add(new_parts[1])
        elif level == "leaf" and old_parts[1] == str(group or "") and old_parts[2] == raw:
            candidates.add(new_parts[2])
    if len(candidates) == 1:
        return next(iter(candidates))
    return raw


def category_is_valid(category: str | None) -> bool:
    value = str(category or "").strip()
    return value in TRANSACTION_CATEGORIES or value in LEGACY_TRANSACTION_CATEGORIES or value in HISTORICAL_CATEGORY_ALIASES


def category_parts(category: str | None) -> dict[str, str | bool | None]:
    raw = str(category or "").strip()
    value = canonicalize_category_path(raw)
    if value == UNCATEGORIZED_PATH:
        return {"path": value, "main": None, "group": None, "leaf": value, "is_transfer": False}
    parts = [part.strip() for part in value.split(" → ", 2)]
    if len(parts) != 3 or value not in CATEGORY_PATHS:
        return {"path": raw or None, "main": None, "group": None, "leaf": raw or None, "is_transfer": None}
    return {"path": value, "main": parts[0], "group": parts[1], "leaf": parts[2], "is_transfer": parts[0] == "Transfers & Non-Expense Movements"}


def legacy_category_for_path(category: str | None) -> str:
    parts = category_parts(category)
    if not parts["main"]:
        value = str(category or UNCATEGORIZED_PATH).strip()
        return value if value in LEGACY_TRANSACTION_CATEGORIES else UNCATEGORIZED_PATH
    main, group, leaf = str(parts["main"]), str(parts["group"]), str(parts["leaf"])
    if main == "Income & Inflows": return "Income / Paycheck"
    if main == "Essential / Fixed Living Expenses":
        if group == "Groceries & Household": return "Groceries"
        if group == "Transportation": return "Transportation / Gas"
        if group == "Healthcare": return "Healthcare / Medical"
        if group == "Utilities & Communications": return "Utilities"
        return "Essential Living"
    if main == "Discretionary Spending":
        if group == "Dining & Food": return "Dining Out"
        if group == "Shopping": return "Shopping"
        if group == "Entertainment & Media": return "Entertainment"
        if group == "Digital Subscriptions & Services": return "Subscription"
        if group == "Travel": return "Travel / Vacation"
        return "Shopping"
    if main == "Maintenance, Education & Business": return "Business / Tax Deductible" if group == "Business" else "Education & Life Events"
    if main == "Financial Management & Wealth": return "Financial Management"
    if main == "Transfers & Non-Expense Movements":
        if leaf == "Credit Card Payment": return "Credit Card Bill Payment"
        if leaf == "P2P Transfer": return "P2P Transfer"
        return "Transfer"
    return UNCATEGORIZED_PATH


__all__ = [
    "CATEGORY_DESCRIPTIONS", "CATEGORY_PATHS", "HISTORICAL_CATEGORY_ALIASES",
    "LEGACY_TRANSACTION_CATEGORIES", "MAIN_CATEGORIES", "TAXONOMY",
    "TRANSACTION_CATEGORIES", "UNCATEGORIZED_PATH", "canonicalize_category_path",
    "canonicalize_taxonomy_choice", "category_is_valid", "category_parts",
    "legacy_category_for_path",
]
