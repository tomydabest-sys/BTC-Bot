import pandas as pd
df = pd.read_json("logs/decisions.jsonl", lines=True)
print(f"Total decisions: {len(df)}")
print("\nTop 15 block reasons:")
print(df["reason"].value_counts().head(15))
print("\nBy strategy:")
print(df.groupby("strategy")["reason"].value_counts().head(30))
print("\nDecisions that reached BUY/SELL:")
print(df[df["decision"].isin(["BUY", "SELL"])][["strategy", "market_id", "edge_bps", "confidence"]].head(20))
