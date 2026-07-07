#!/usr/bin/env python3
"""
跨境电商定价计算引擎

根据人民币成本（单价+运费）和实时汇率，倒推计算符合平台佣金、
目标利润率及心理定价规则（尾数 .49/.99）的最终美元售价。

集成方式:
  from pricing_calculator import PricingEngine
  engine = PricingEngine()
  result = engine.calculate(cny_unit=26.16, cny_ship=32.00)
  print(result["final_pricing"]["final_price_usd"])  # → 17.49
"""

import math
import json
from typing import Optional
from dataclasses import dataclass, asdict


@dataclass
class PricingResult:
    input_data: dict
    calculations: dict
    final_pricing: dict


class PricingEngine:
    """定价计算引擎"""

    COMMISSION_RATE = 0.15   # 平台佣金
    PROFIT_RATE = 0.35       # 目标利润率
    COST_RATIO = 1 - COMMISSION_RATE - PROFIT_RATE  # = 0.5

    def __init__(self, exchange_rate: Optional[float] = None):
        """
        Args:
            exchange_rate: CNY/USD 汇率。为 None 时自动从 API 获取。
        """
        self._exchange_rate = exchange_rate

    @property
    def exchange_rate(self) -> float:
        if self._exchange_rate is None:
            self._exchange_rate = self._fetch_exchange_rate()
        return self._exchange_rate

    def _fetch_exchange_rate(self) -> float:
        """从免费汇率 API 获取实时 CNY/USD 汇率"""
        import requests
        try:
            r = requests.get(
                "https://cdn.jsdelivr.net/npm/@fawazahmed0/"
                "currency-api@latest/v1/currencies/usd.json",
                timeout=10,
            )
            data = r.json()
            return float(data["usd"]["cny"])
        except Exception:
            # 兜底: 使用常见汇率
            return 7.25

    def calculate(
        self,
        cny_unit: float,
        cny_ship: float,
        exchange_rate: Optional[float] = None,
    ) -> dict:
        """
        执行定价计算。

        Args:
            cny_unit: 产品单价 (CNY)
            cny_ship: 运费 (CNY)
            exchange_rate: 汇率, 不传则用引擎默认汇率

        Returns:
            dict: 完整计算结果 (JSON 格式)
        """
        rate = exchange_rate if exchange_rate is not None else self.exchange_rate
        total_cost_cny = cny_unit + cny_ship

        # Step 1: 基础美元成本
        cost_usd = total_cost_cny / rate

        # Step 2: 理论原始售价
        raw_price = cost_usd / self.COST_RATIO

        # Step 3: 心理定价 (x.49 / x.99)
        final_price = self._apply_psychological_pricing(raw_price)

        # Step 4: 兜底校验
        final_price = self._validate_and_correct(final_price, raw_price)

        # 计算佣金和利润
        commission_usd = round(final_price * self.COMMISSION_RATE, 2)
        profit_usd = round(final_price * self.PROFIT_RATE, 2)
        actual_cost_ratio = cost_usd / final_price
        actual_margin = 1 - self.COMMISSION_RATE - actual_cost_ratio

        return {
            "input_data": {
                "cny_unit": cny_unit,
                "cny_ship": cny_ship,
                "exchange_rate": round(rate, 4),
            },
            "calculations": {
                "total_cost_cny": round(total_cost_cny, 2),
                "cost_usd": round(cost_usd, 4),
                "raw_price_usd": round(raw_price, 4),
            },
            "final_pricing": {
                "final_price_usd": final_price,
                "commission_usd": commission_usd,
                "profit_usd": profit_usd,
                "actual_profit_margin": f"{actual_margin:.1%}",
            },
        }

    def _apply_psychological_pricing(self, raw_price: float) -> float:
        """心理定价: 映射到 .49 或 .99"""
        int_part = math.floor(raw_price)
        decimal_part = raw_price - int_part

        if decimal_part <= 0.49:
            return int_part + 0.49
        else:
            return int_part + 0.99

    def _validate_and_correct(
        self, final_price: float, raw_price: float
    ) -> float:
        """
        兜底校验:
        1. 防亏损: final_price < raw_price 时强制进位
        2. 极低价: final_price < 0.49 时设为 0.99
        """
        # 极低价校验
        if final_price < 0.49:
            return 0.99

        # 防亏损校验: final_price 不能低于 raw_price
        if final_price < raw_price:
            int_part = math.floor(final_price)
            decimal_part = final_price - int_part
            # 若当前是 .49, 升级到 .99; 若已是 .99, 整数+1 取 .49
            if abs(decimal_part - 0.49) < 0.01:
                return int_part + 0.99
            elif abs(decimal_part - 0.99) < 0.01:
                return int_part + 1.49
            else:
                # fallback: 直接向上取 .99
                return int_part + 0.99

        return final_price


# ── 命令行入口 ──
if __name__ == "__main__":
    import sys

    if len(sys.argv) >= 3:
        cny_unit = float(sys.argv[1])
        cny_ship = float(sys.argv[2])
        rate = float(sys.argv[3]) if len(sys.argv) > 3 else None
    else:
        print("用法: python pricing_calculator.py <单价> <运费> [汇率]")
        print("示例: python pricing_calculator.py 26.16 32.00")
        sys.exit(1)

    engine = PricingEngine(exchange_rate=rate)
    result = engine.calculate(cny_unit=cny_unit, cny_ship=cny_ship)
    print(json.dumps(result, indent=2, ensure_ascii=False))
