from faker import Faker

fake = Faker("en_US")
Faker.seed(42)

# Faker is used only for synthetic identity/display fields.
# Policy-sensitive fields such as loyalty tier, fraud flags,
# refund history, dates, prices, categories, and final-sale
# status are curated intentionally so demo outcomes are deterministic.
