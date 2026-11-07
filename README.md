# WowProfessionAssistant
## Setup
- python -m venv .venv
- source .venv/bin/activate
- pip install -r requirements.txt
- playwright install

Install Auctionator addon
## Usage

1. Symlink `WTF/Accout/<ACCOUNT_NAME>/SavedVariables/Auctionator.lua` to `./prices/Auctionator.lua` (only once for setup)
1. Run in-game Full Scan of the AH
1. Run `./update-prices.py auctionator` to port price data to JSON structure
1. Run `./update-prices.py <profession|all>`
1. Profit
