# import MetaTrader5 as mt5

# def validate_symbol(symbol):
#     info = mt5.symbol_info(symbol)
#     if info is None:
#         raise ValueError(f"Symbol {symbol} not found in Market Watch.")
#     if not info.visible:
#         mt5.symbol_select(symbol, True)  # force-add to Market Watch


# def initialize_mt5():
#     if not mt5.initialize():
#         raise RuntimeError(f"MT5 initialization failed: {mt5.last_error()}")

def main():
    print("Hello from londonopen!")


if __name__ == "__main__":
    main()
