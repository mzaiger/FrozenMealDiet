import pandas as pd

# Load original dataset
df = pd.read_csv("frozen_food.csv")

# Exclude categories
categories_to_remove = [
    "Frozen Desserts", 
    "Frozen Meat & Seafood", 
    "Frozen Produce", 
    "Frozen Potatoes"
]
filtered_df = df[~df['CATEGORY'].isin(categories_to_remove)]

# Deduplicate by SKU (keep first occurrence)
deduped_df = filtered_df.drop_duplicates(subset=['SKU'], keep='first')

# Save directly to your machine
deduped_df.to_csv("frozen_food_deduped.csv", index=False)

print(f"Done! Saved {len(deduped_df)} unique items to frozen_food_deduped.csv")
