# Submission package

## Title

Physical-Goods Delivery-Condition Vault: Escrow Gated on Validator Image Consensus

## Notes (963 / 1000 characters)

Escrow for P2P physical-goods trades that releases only when validator
consensus judges the delivered item's photo against the seller's listing
photo -- never a human, never a single LLM call, never the platform. Two
independent nondet surfaces: image-based condition adjudication (banded
MATCH/MINOR/MAJOR/NOT_RECEIVED, never a raw float) and a separate
web-fetched carrier-tracking classification that lets a seller claim
payment if the buyer goes silent after real delivery. A bonded,
single-round contest ladder lets either party force a second adversarial
judgment before funds finalize; the loser's bond is forfeited to the
counterparty.

Escrow ledger fields are zeroed before every transfer
(checks-effects-interactions), so double-spend is structurally impossible
-- verified in 58 direct tests. Live on StudioNet: a contested deal
converged on MATCH twice (identical photos), upheld on re-adjudication, and
paid out correctly in one permissionless call.

Character count verified with `python3 -c "print(len(open('notes.txt').read().strip()))"` -> 963.

## Evidence links

- GitHub repo: https://github.com/zoefunds/Delivery-vault
- Deployed contract (StudioNet): `0x6C8b6928EeFE8121A4A9265d74f86EEe55C1C054`
- Explorer: https://genlayer-explorer.vercel.app/contracts/0x6C8b6928EeFE8121A4A9265d74f86EEe55C1C054
- Studio import: use the address above with "Import contract" in GenLayer Studio pointed at StudioNet.

## Git hygiene

- Single commit, no AI/agent co-author or `Generated with` trailer
  (verified: `git log -1 --format='%B' | grep -i "co-authored\|claude\|generated with"` -> no match).
