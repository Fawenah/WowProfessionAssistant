# WowProfessionAssistant
## Setup
1. python -m venv .venv
1. source .venv/bin/activate
1. pip install -r requirements.txt
1. Install [Auctionator addon](https://www.curseforge.com/wow/addons/auctionator)
1. Symlink `WTF/Accout/<ACCOUNT_NAME>/SavedVariables/Auctionator.lua` to `./prices/Auctionator.lua`

## Usage
1. Run in-game Full Scan of the AH
1. Run `./update-prices.py auctionator` to port price data to JSON structure
1. Run `./profit-craft.py <profession|all>` to show what's profitable
1. Profit
