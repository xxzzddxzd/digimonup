"""Defaults taken from Charles capture (iOS 1.0.2)."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AccountConfig:
    client_id: str = "64cb303a066d988d9581da8d112687f236a51b0c3346d4a0fd37caf8759f00aa"
    device_id: str = "BF8BFC26-10E0-4107-B51B-E89D140B2196"
    platform_user_id: str = "8E986415-A2DC-4903-A4B8-7DEA92B07F7A"
    device_model: str = "iPhone14,3"
    operating_system: str = "iOS 16.1.1"
    ad_id: str = "00000000-0000-0000-0000-000000000000"
    push_token: str = ""
    is_guest: bool = False
    country: int = 102
    store_region_code: int = 250
    region_type: int = 1
    # Content-data hash used as _dataNo after game data is ready.
    data_no: str = "ded91528d845767b07f5a6ce30f85e6bddc314e65dce0f91d8627d9aa9f83e08"
    # Preferred server after auth (capture used 14).
    preferred_server_num: int = 14
    # Capture-time main-story progress (best estimate).
    capture_stage: int = 23
    capture_sector: int = 2
    capture_region: int = 1


@dataclass
class ClientConfig:
    base_url: str = "https://dm-content.dgup.channel.or.jp"
    version: str = "1.0.2"
    unity_version: str = "6000.3.11f1"
    accept_language: str = "zh-CN,zh-Hans;q=0.9"
    timeout: float = 30.0
    account: AccountConfig = field(default_factory=AccountConfig)
    load_saved_account: bool = True

    def __post_init__(self) -> None:
        if self.load_saved_account:
            try:
                from .account_store import apply_account_to_config, load_account_file

                data = load_account_file()
                if data:
                    apply_account_to_config(self, data)
            except Exception:
                # Keep hardcoded defaults if account.json is missing/broken.
                pass

    @property
    def user_agent(self) -> str:
        a = self.account
        return (
            f"{self.version}; IPhonePlayer; Unity/{self.unity_version}; "
            f"DeviceModel/{a.device_model}; OS/{a.operating_system}"
        )
