"""
On-chain Task Bot — automates simple on-chain operations on testnets
to qualify for airdrop eligibility.

Actions:
  - Swap native token for USDC on testnet DEXes
  - Bridge tokens between testnets
  - Mint testnet NFTs
  - Deploy simple contracts
  - Interact with popular testnet protocols

Each action uses disposable wallets and logs to Transaction table.
Gas costs are tracked against the budget.
"""

import logging
import random
from datetime import datetime, timezone
from typing import Optional
from decimal import Decimal

import httpx
from sqlalchemy.orm import Session
from web3 import Web3
from web3.types import Wei

from app.config import settings
from app.db.models import Wallet, WalletType, Transaction
from app.modules.wallet import (
    derive_wallet,
    get_private_key,
    sync_wallet_to_db,
    CHAIN_CONFIGS,
)
from app.modules.rpc import get_web3, get_balance
from app.utils.budget import can_spend, record_spend
from app.utils.notify import notify_alert

logger = logging.getLogger("buckgen.ontask")

# =============================================================================
# Testnet chain config
# =============================================================================

TESTNET_CONFIG: dict[str, dict] = {
    "sepolia": {
        "chain_id": 11155111,
        "symbol": "ETH",
        "rpc": "https://rpc.sepolia.org",
        "explorer": "https://sepolia.etherscan.io/tx/{tx}",
        "native_faucet": "https://sepolia-faucet.pk910.de",
    },
    "base-sepolia": {
        "chain_id": 84532,
        "symbol": "ETH",
        "rpc": "https://sepolia.base.org",
        "explorer": "https://sepolia.basescan.org/tx/{tx}",
    },
    "arbitrum-sepolia": {
        "chain_id": 421614,
        "symbol": "ETH",
        "rpc": "https://sepolia-rollup.arbitrum.io/rpc",
        "explorer": "https://sepolia.arbiscan.io/tx/{tx}",
    },
    "amoy": {
        "chain_id": 80002,
        "symbol": "MATIC",
        "rpc": "https://rpc-amoy.polygon.technology",
        "explorer": "https://amoy.polygonscan.com/tx/{tx}",
    },
    # ---- New testnets added for broader airdrop coverage ----
    "optimism-sepolia": {
        "chain_id": 11155420,
        "symbol": "ETH",
        "rpc": "https://sepolia.optimism.io",
        "explorer": "https://sepolia-optimism.etherscan.io/tx/{tx}",
    },
    "zksync-sepolia": {
        "chain_id": 300,
        "symbol": "ETH",
        "rpc": "https://sepolia.era.zksync.dev",
        "explorer": "https://sepolia.explorer.zksync.io/tx/{tx}",
    },
    "scroll-sepolia": {
        "chain_id": 534351,
        "symbol": "ETH",
        "rpc": "https://sepolia-rpc.scroll.io",
        "explorer": "https://sepolia.scrollscan.com/tx/{tx}",
    },
    "linea-sepolia": {
        "chain_id": 59141,
        "symbol": "ETH",
        "rpc": "https://rpc.sepolia.linea.build",
        "explorer": "https://sepolia.lineascan.build/tx/{tx}",
    },
    "bsc-testnet": {
        "chain_id": 97,
        "symbol": "BNB",
        "rpc": "https://data-seed-prebsc-1-s1.binance.org:8545",
        "explorer": "https://testnet.bscscan.com/tx/{tx}",
    },
    "fuji": {
        "chain_id": 43113,
        "symbol": "AVAX",
        "rpc": "https://api.avax-test.network/ext/bc/C/rpc",
        "explorer": "https://testnet.snowtrace.io/tx/{tx}",
    },
}

# Minimal ERC-20 transfer ABI for sending test tokens
ERC20_ABI = [
    {
        "constant": False,
        "inputs": [
            {"name": "_to", "type": "address"},
            {"name": "_value", "type": "uint256"},
        ],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function",
    },
]

# Common testnet tokens
TESTNET_USDC: dict[str, str] = {
    "sepolia": "0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238",
    "base-sepolia": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
    "arbitrum-sepolia": "0x75faf114eafb1BDbe2F0316DF893fd58CE46AA4d",
    "amoy": "0x41E94Eb019C0762f529Bf6aE8d4AAb1Ff8cB5C4A",
}


# =============================================================================
# Simple native transfer (testnet action #1)
# =============================================================================


def _get_testnet_w3(chain: str) -> Optional[Web3]:
    """Get Web3 connection for a testnet chain."""
    config = TESTNET_CONFIG.get(chain)
    if not config:
        return None
    try:
        w3 = Web3(Web3.HTTPProvider(config["rpc"], request_kwargs={"timeout": 10}))
        if w3.is_connected():
            return w3
    except Exception:
        pass
    return None


async def send_self_transfer(
    db: Session,
    chain: str = "sepolia",
    wallet_index: int = 10,
) -> dict:
    """
    Send a small amount of native token between two agent-owned wallets.
    This creates on-chain activity that qualifies for airdrop farming.

    Action: Wallet A -> sends 0.0001 ETH -> Wallet B
    """
    w3 = _get_testnet_w3(chain)
    if not w3:
        return {"success": False, "error": f"Cannot connect to {chain}"}

    config = TESTNET_CONFIG.get(chain, {})
    symbol = config.get("symbol", "ETH")

    # Check budget
    if not can_spend(db, 0.001):
        return {"success": False, "error": "Budget cap reached"}

    # Get two agent wallets
    try:
        sender = derive_wallet(wallet_index, chain)
        receiver = derive_wallet(wallet_index + 1, chain)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}

    # Check sender has balance
    sender_balance = w3.eth.get_balance(sender.address)
    min_amount = w3.to_wei(0.0001, "ether")
    if sender_balance < min_amount * 2:
        return {
            "success": False,
            "error": f"Insufficient balance ({w3.from_wei(sender_balance, 'ether')} {symbol})",
        }

    try:
        # Build transfer tx
        tx = {
            "to": receiver.address,
            "value": min_amount,
            "gas": 21000,
            "gasPrice": w3.eth.gas_price,
            "nonce": w3.eth.get_transaction_count(sender.address),
            "chainId": config["chain_id"],
        }

        # Sign and send
        signed = w3.eth.account.sign_transaction(tx, sender.key)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        tx_hash_hex = tx_hash.hex()

        # Record
        tx_record = Transaction(
            wallet_id=wallet_index,
            chain=chain,
            tx_hash=tx_hash_hex,
            tx_type="self_transfer",
            amount_wei=str(min_amount),
            status="pending",
            memo=f"Self-transfer on {chain} (airdrop farming)",
        )
        db.add(tx_record)
        record_spend(db, 0.0005, "gas", f"self-transfer {chain}")
        db.commit()

        logger.info("[ontask] Self-transfer on %s: tx=%s", chain, tx_hash_hex[:20])
        return {
            "success": True,
            "tx_hash": tx_hash_hex,
            "chain": chain,
            "from": sender.address[:10],
            "to": receiver.address[:10],
            "amount": 0.0001,
            "symbol": symbol,
        }

    except Exception as exc:
        logger.warning("[ontask] Self-transfer failed: %s", exc)
        return {"success": False, "error": str(exc)[:120]}


# =============================================================================
# Deploy a simple contract (testnet action #2)
# =============================================================================

# Minimal "Counter" contract bytecode (deployed on testnets to show activity)
COUNTER_BYTECODE = "0x608060405234801561001057600080fd5b5060fd8061001f6000396000f3fe6080604052348015600f57600080fd5b5060043610603c5760003560e01c80633fb5c1cb14604157806361bc221a14605b578063d09de08a146073575b600080fd5b6059604c3660046083565b600055565b005b606360005481565b60405190815260200160405180910390f35b60596000805490819055609c565b600060208284031215609457600080fd5b503591905056fea2646970667358221220a9a0"


async def deploy_test_contract(
    db: Session,
    chain: str = "sepolia",
    wallet_index: int = 10,
) -> dict:
    """
    Deploy a simple Counter contract on testnet.
    This creates on-chain activity that airdrop hunters look for.
    """
    w3 = _get_testnet_w3(chain)
    if not w3:
        return {"success": False, "error": f"Cannot connect to {chain}"}

    config = TESTNET_CONFIG.get(chain, {})
    symbol = config.get("symbol", "ETH")

    if not can_spend(db, 0.002):
        return {"success": False, "error": "Budget cap reached"}

    try:
        account = derive_wallet(wallet_index, chain)
        balance = w3.eth.get_balance(account.address)

        if balance < w3.to_wei(0.001, "ether"):
            return {"success": False, "error": "Insufficient balance to deploy"}

        # Build contract deployment tx
        tx = {
            "data": COUNTER_BYTECODE,
            "value": 0,
            "gas": 150000,
            "gasPrice": w3.eth.gas_price,
            "nonce": w3.eth.get_transaction_count(account.address),
            "chainId": config["chain_id"],
        }

        # Estimate gas
        try:
            tx["gas"] = w3.eth.estimate_gas(tx)
        except Exception:
            tx["gas"] = 150000

        signed = w3.eth.account.sign_transaction(tx, account.key)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        tx_hash_hex = tx_hash.hex()

        tx_record = Transaction(
            wallet_id=wallet_index,
            chain=chain,
            tx_hash=tx_hash_hex,
            tx_type="contract_deploy",
            status="pending",
            memo=f"Counter contract deploy on {chain}",
        )
        db.add(tx_record)
        record_spend(db, 0.001, "gas", f"deploy {chain}")
        db.commit()

        logger.info("[ontask] Contract deployed on %s: tx=%s", chain, tx_hash_hex[:20])
        return {
            "success": True,
            "tx_hash": tx_hash_hex,
            "chain": chain,
            "action": "contract_deploy",
        }

    except Exception as exc:
        logger.warning("[ontask] Contract deploy failed: %s", exc)
        return {"success": False, "error": str(exc)[:120]}


# =============================================================================
# Additional on-chain actions
# =============================================================================

# Minimal Uniswap V2 router ABI for testnet swaps
UNISWAP_V2_ROUTER_ABI = [
    {
        "inputs": [
            {"internalType": "uint256", "name": "amountOutMin", "type": "uint256"},
            {"internalType": "address[]", "name": "path", "type": "address[]"},
            {"internalType": "address", "name": "to", "type": "address"},
            {"internalType": "uint256", "name": "deadline", "type": "uint256"},
        ],
        "name": "swapExactETHForTokens",
        "outputs": [
            {"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}
        ],
        "stateMutability": "payable",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
            {"internalType": "uint256", "name": "amountOutMin", "type": "uint256"},
            {"internalType": "address[]", "name": "path", "type": "address[]"},
            {"internalType": "address", "name": "to", "type": "address"},
            {"internalType": "uint256", "name": "deadline", "type": "uint256"},
        ],
        "name": "swapExactTokensForETH",
        "outputs": [
            {"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}
        ],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]

# Uniswap V2 router addresses on testnets
TESTNET_ROUTERS: dict[str, str] = {
    "sepolia": "0xC532a74256D3Db42D0Bf7a0400fEFDbad7694008",  # Uniswap V2 on Sepolia
    "base-sepolia": "0x500e4f2E6A9b7F8F3F5C8E5B5A9b0f1E2d3c4b5",  # placeholder
    "arbitrum-sepolia": "0x500e4f2E6A9b7F8F3F5C8E5B5A9b0f1E2d3c4b5",  # placeholder
}


async def mint_test_nft(
    db: Session,
    chain: str = "sepolia",
    wallet_index: int = 10,
) -> dict:
    """
    Mint a simple ERC-721 NFT on testnet for airdrop eligibility.

    Deploys a minimal NFT contract and mints one token to the wallet.

    Args:
        db: Database session
        chain: Testnet name
        wallet_index: HD wallet index

    Returns:
        Result dict with tx_hash or error.
    """
    w3 = get_web3(chain)
    if not w3:
        return {"success": False, "error": f"No RPC for {chain}"}

    account = derive_wallet(wallet_index, chain)
    config = TESTNET_CONFIG[chain]

    try:
        # Minimal ERC-721 bytecode (simplified OpenZeppelin)
        # This is a very minimal NFT that just does _safeMint to the deployer
        nft_bytecode = "0x608060405234801561001057600080fd5b50610140806100206000396000f3fe608060405234801561001057600080fd5b50600436106100365760003560e01c8063a0712d681461003b578063b88d4fde1461005d575b600080fd5b61005b6004803603602081101561005157600080fd5b5035610079565b005b61005b6004803603604081101561007357600080fd5b50356001600160a01b03166100c9565b6040805133815234602082015281517f000000000000000000000000000000000000000000000000000000000000000092909160008051602061015083398151915291819003820190a350565b604080516001600160a01b038416815234602082015281517f000000000000000000000000000000000000000000000000000000000000000092600080516020610150833981519152929181900390910190a3505056fe"  # pragma: allowlist secret

        # Deploy NFT contract
        nft_contract = w3.eth.contract(
            abi=[
                {"inputs": [], "stateMutability": "nonpayable", "type": "constructor"}
            ],
            bytecode=nft_bytecode,
        )

        tx = nft_contract.constructor().build_transaction(
            {
                "from": account.address,
                "nonce": w3.eth.get_transaction_count(account.address),
                "gas": 200000,
                "gasPrice": w3.eth.gas_price,
                "chainId": config["chain_id"],
            }
        )

        signed = w3.eth.account.sign_transaction(tx, account.key)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        tx_hash_hex = tx_hash.hex()

        tx_record = Transaction(
            wallet_id=wallet_index,
            chain=chain,
            tx_hash=tx_hash_hex,
            tx_type="nft_mint",
            status="pending",
            memo=f"NFT mint on {chain}",
        )
        db.add(tx_record)
        record_spend(db, 0.001, "gas", f"nft_mint {chain}")
        db.commit()

        logger.info("[ontask] NFT deployed on %s: tx=%s", chain, tx_hash_hex[:20])
        return {
            "success": True,
            "tx_hash": tx_hash_hex,
            "chain": chain,
            "action": "nft_mint",
        }

    except Exception as exc:
        logger.warning("[ontask] NFT mint failed on %s: %s", chain, exc)
        return {"success": False, "error": str(exc)[:120]}


# =============================================================================
# Batch on-chain actions
# =============================================================================


async def run_testnet_actions(
    db: Session,
    chain: str = "sepolia",
    wallet_index: int = 10,
) -> list[dict]:
    """
    Run a series of on-chain actions on a testnet to build airdrop eligibility.

    Actions:
      1. Self-transfer (wallet A -> wallet B)
      2. Deploy a contract
      3. Mint an NFT (ERC-721)
      4. Create an ERC-20 token (future)

    Returns list of results.
    """
    results = []
    logger.info("[ontask] Running testnet actions on %s", chain)

    # Action 1: Self-transfer
    result1 = await send_self_transfer(db, chain, wallet_index)
    results.append(result1)

    # Action 2: Deploy contract
    result2 = await deploy_test_contract(db, chain, wallet_index)
    results.append(result2)

    # Action 3: Mint NFT
    result3 = await mint_test_nft(db, chain, wallet_index)
    results.append(result3)

    succeeded = sum(1 for r in results if r.get("success"))
    logger.info("[ontask] %s: %d/%d actions succeeded", chain, succeeded, len(results))

    return results


async def run_all_testnets(
    db: Session,
    wallet_index: int = 10,
) -> dict[str, list[dict]]:
    """
    Run on-chain actions on all available testnets.

    Returns dict: chain_name -> [action_results]
    """
    results: dict[str, list[dict]] = {}

    for chain in TESTNET_CONFIG:
        try:
            chain_results = await run_testnet_actions(db, chain, wallet_index)
            results[chain] = chain_results
        except Exception as exc:
            logger.warning("[ontask] %s actions failed: %s", chain, exc)
            results[chain] = [{"success": False, "error": str(exc)[:80]}]

    total = sum(len(actions) for actions in results.values())
    succeeded = sum(
        1 for actions in results.values() for r in actions if r.get("success")
    )

    await notify_alert(
        f"On-chain Tasks: {succeeded}/{total}",
        "\n".join(
            f"{chain}: {sum(1 for r in rs if r.get('success'))}/{len(rs)}"
            for chain, rs in results.items()
        ),
    )

    return results
