import numpy as np
import pandas as pd
from pandas.tseries.offsets import YearBegin, BDay, MonthBegin
import pandas_datareader.data as web
from math import sqrt
from datetime import date, time, datetime
import os
import warnings

import yfinance as yfin


class Metrics:
    """
    Metrics such as market capitalization, value of stocks traded, turnover ratio to capitalization, volatility.
    """

    ADJ_CLOSE = 'Close'
    CLOSE = 'Close'
    VOLUME = 'Volume'
    CAPITALIZATION = 'Capitalization'
    TOTAL_VALUE = 'Total value'
    TURNOVER = 'Turnover ratio'
    MKT_SHARE = 'Market share (%)'
    VOLATILITY = 'Volatility'
    TRADING_DAYS_IN_YEAR = 252
    TO_ANNUAL_MULTIPLIER = sqrt(TRADING_DAYS_IN_YEAR)

    def __init__(self, tickers, stock_index=None, start=None, hist_shares_outs=None):
        """
        Constructs a Metrics object out of a list of ticker symbols and an optional index ticker

        :param tickers: a dictionary representing all the ticker symbols making up a stock market that this class
                        derives metrics for. Each key represents a ticker symbol. Each value designates the start and
                        end day of the stock represented by the ticker being added or removed. An end value of None
                        implies it's still part of the market, a start value of None designates it becoming part of
                        the marke before 'start'
        :param stock_index: an optional ticker symbol of the index that corresponds to the market represented
                            by the 'tickers' parameter
        :param start: (string, int, date, datetime, Timestamp) – Starting date. Parses different kinds of date
                       representations (e.g., ‘JAN-01-2010’, ‘1/1/10’, ‘Jan, 1, 1980’). Defaults to 5 years before
                       current date.
        :param hist_shares_outs: a dictionary representing historical shares outstanding for delisted tickers.
                            Each key represents a ticker symbol. Each value is a panda Series designating shares
                            outstanding on certain days.
        """
        self.ticker_symbols = tickers
        self.tickers = yfin.Tickers(list(self.ticker_symbols.keys()))
        # When calculating market capitalization and trading volumes, we need to use nominal close prices
        # and not adjusted close prices
        self.data = self.tickers.download(start=start, auto_adjust=False, actions=False, ignore_tz=True)
        self.data = self.data.loc[:, ([Metrics.CLOSE, Metrics.VOLUME])]

        # In case some stocks captured in the tickers list were not trading during the date range,
        # I assume they were trading using their first price.
        self.data.bfill(inplace=True)

        # Unfortunately Yahoo-Finance provides dividend yield only for the most recent trading day
        self.dividend_yield = {}
        for ticker in self.get_current_components():
            dividend_yield = self.tickers.tickers[ticker].info.get('dividendYield')
            self.dividend_yield[ticker] = 0. if dividend_yield is None else dividend_yield

        subset = self.data.loc[:, (self.CLOSE,)]
        delisted_tickers = subset.columns[subset.isna().all()]
        for delisted_ticker in delisted_tickers:
            if os.path.isfile(os.path.expanduser(f'./stock_market/historical_equity_data/{delisted_ticker}.cvs')):
                f = pd.read_csv(f'./stock_market/historical_equity_data/{delisted_ticker}.cvs', index_col=0)
            else:
                # Filling in the gaps with Alpha Vantage API for close prices and volumes of delisted shares
                # Please request your own API Key in order to make the below call work
                f = web.DataReader(delisted_ticker, 'av-daily', start=start, api_key=os.getenv('ALPHAVANTAGE_API_KEY'))
                f.to_csv(f'./stock_market/historical_equity_data/{delisted_ticker}.cvs')

            f = f.set_axis(pd.DatetimeIndex(f.index, self.data.index.freq))
            f = f.loc[self.data.index[0]:, ['close', 'volume']]
            f.columns = pd.MultiIndex.from_tuples(list(zip([self.CLOSE, self.VOLUME], [delisted_ticker]*2)))
            self.data.loc[f.index, ([self.CLOSE, self.VOLUME], delisted_ticker)] = f

            # In case delisting took place before a formal removal from the market, I replicate the last closing price
            # and assign a volume of 0 to the trading days leading up to the delisting
            if f.index[-1] < self.ticker_symbols[delisted_ticker][1]:
                self.data.loc[f.index[-1] + BDay(1):self.ticker_symbols[delisted_ticker][1],
                              ([self.CLOSE, self.VOLUME], delisted_ticker)] = (f.iloc[-1,0], 0.)

        self.shares_outstanding = {}
        for ticker in self.ticker_symbols.keys():

            # For delisted shares, Yahoo-Finance doesn't report any data
            if ticker in delisted_tickers:
                if hist_shares_outs is None or ticker not in hist_shares_outs:
                    raise ValueError(f'No data on shares outstanding for {ticker}')
                shares_outst = hist_shares_outs[ticker]

            # In case the historical shares outstanding dictionary has an entry, use it in preference to
            # Yahoo-Finance's API
            elif hist_shares_outs is not None and ticker in hist_shares_outs:
                shares_outst = hist_shares_outs[ticker]

            else:
                shares_outst = self.tickers.tickers[ticker].get_shares_full(start=start).tz_localize(None)
                # Unfortunately Yahoo-Finance occasionally reports duplicate values for shares outstanding
                # for the same date. In such cases I take the most recent value.
                shares_outst = shares_outst.groupby(level=0).last()

                # Correction for the shares outstanding for companies that have multiple classes of shares that
                # are listed, e.g. Alphabet's Class A 'GOOGL' and Class C 'GOOG' stocks or 'BRK-A' and 'BRK-B'
                shares_outstanding = self.tickers.tickers[ticker].info.get('sharesOutstanding')
                if shares_outstanding is not None and shares_outstanding * 1.2 < shares_outst.iloc[-1].item():
                    shares_outstanding2 = self.tickers.tickers[ticker].info.get('impliedSharesOutstanding')
                    if shares_outstanding2 is None:
                        shares_outstanding2 = shares_outst.iloc[-1].item()
                    print('Correcting the number of shares outstanding for {:s} from {:d} to {:d}'
                          .format(ticker, shares_outst.iloc[-1].item(),
                                  int(shares_outst.iloc[-1].item() * shares_outstanding / shares_outstanding2)))
                    shares_outst *= shares_outstanding / float(shares_outstanding2)
                    shares_outst = shares_outst.astype('int64')

            # Yahoo-Finance doesn't report shares outstanding for each trading day. I compensate for it by rolling
            # forward the most recent reported value and then rolling backward the earliest available number.
            missing_dates = self.data.index.difference(shares_outst.index)
            shares_outst = pd.concat([shares_outst, pd.Series(np.nan, index=missing_dates)]).sort_index()
            shares_outst = shares_outst.ffill().bfill()

            # Getting rid of extraneous dates
            self.shares_outstanding[ticker] = shares_outst.loc[self.data.index]

        warnings.filterwarnings('ignore', message='DataFrame is highly fragmented*',
                                category=pd.errors.PerformanceWarning)
        self.capitalization = pd.DataFrame(0., index=self.data.index,
                                           columns=[Metrics.CAPITALIZATION, Metrics.TURNOVER])
        self.forward_dividend_yield = 0.
        for ticker in self.ticker_symbols.keys():
            # print('Processing {:s}'.format(ticker))
            df = self.data.loc(axis=1)[:, ticker].droplevel(1, axis=1)

            (st, ed) = self.ticker_symbols[ticker]
            # Turnover = Closing price x Volume
            self.capitalization.loc[st:ed, Metrics.TURNOVER]\
                += df.loc[st:ed,Metrics.CLOSE] * df.loc[st:ed,Metrics.VOLUME]

            # Capitalization = Closing price x Shares outstanding.
            self.capitalization.loc[st:ed, Metrics.CAPITALIZATION]\
                += df.loc[st:ed,Metrics.CLOSE] * self.shares_outstanding[ticker].loc[st:ed]
            self.capitalization.loc[st:ed, ticker]\
                = df.loc[st:ed,Metrics.CLOSE] * self.shares_outstanding[ticker].loc[st:ed]

            # Most recent forward dividend yield
            if ticker in self.get_current_components():
                self.forward_dividend_yield += (df.iloc[:,0] * self.shares_outstanding[ticker]).iloc[-1]\
                    * self.dividend_yield[ticker]

        self.forward_dividend_yield /= self.capitalization.iloc[-1,0]

        # Given that a stock index is used for calculating the volatility, we need to use adjusted close prices.
        if stock_index is not None:
            self.stock_index_data = yfin.download(
                stock_index, start=start, auto_adjust=True, actions=False, ignore_tz=True)\
                    .loc[:, Metrics.ADJ_CLOSE]

    def get_capitalization(self, frequency='M', tickers=None):
        """
        Calculates the capitalization of a given market or a subset of stocks over time. Downsamples if a less
        granular frequency than daily is specified. Takes an average capitalization over periods implied by
        the 'frequency' parameter.

        :param frequency: a standard Pandas frequency designator
            https://pandas.pydata.org/pandas-docs/stable/user_guide/timeseries.html#timeseries-offset-aliases
        :param tickers: a list of one or more ticker symbols
        :returns: a pd.Series object capturing the capitalization of the market
        """
        if tickers is None:
            return self.capitalization.loc[:, Metrics.CAPITALIZATION].resample(frequency).mean().dropna()
        else:
            ret = pd.Series(0., index=self.data.index)
            for ticker in tickers:
                (st, ed) = self.ticker_symbols[ticker]
                ret += self.data.loc[st:ed, (Metrics.CLOSE, ticker)] * self.shares_outstanding[ticker].loc[st:ed]
            return ret.resample(frequency).mean().ret.dropna()

    def get_top_n_capitalization_companies_for_day(self, n, dt=None):
        """
        Calculates the top-n capitalization companies for the market.

        :param n: the number of maximum capitalization companies to return
        :param dt: (string, int, date, datetime, Timestamp) – the date for which to return the top-n capitalization,
                   if None, the most recent business day is used
        :returns: a pd.Series object capturing the top-n capitalization companies for the market along with the
                  percentage share of their capitalization in the market. The pd.Series is indexed by the ticker
                  symbols.
        """
        if dt is None:
            dt = self.data.index[-1]
        dt = BDay(0).rollback(dt) if BDay(0).rollback(dt) <= self.data.index[-1] else self.data.index[-1]
        topn = self.capitalization.loc[dt, self.capitalization.columns[2:]].nlargest(n).to_frame(self.CAPITALIZATION)
        topn[self.MKT_SHARE] = topn.iloc[:, 0] / self.capitalization.loc[dt, self.CAPITALIZATION]
        return topn

    def get_top_n_capitalization_companies_for_month(self, n, dt=None):
        """
        Calculates the top-n capitalization companies for the market.

        :param n: the number of maximum capitalization companies to return
        :param dt: (string, int, date, datetime, Timestamp) – the date for whose month to return the topn capitalization
                    if None, the most recent business day is used
        :returns: a pd.Series object capturing the top-n capitalization companies for the market along with the
                  percentage share of their capitalization in the market. The pd.Series is indexed by the ticker
                  symbols.
        """
        if dt is None:
            dt = self.data.index[-1]
        dt = MonthBegin(0).rollback(dt) if MonthBegin(0).rollback(dt) <= self.data.index[-1] else self.data.index[-1]
        resampled_cap = self.capitalization.resample('MS').mean()
        topn = resampled_cap.loc[dt, resampled_cap.columns[2:]].nlargest(n).to_frame(self.CAPITALIZATION)
        topn[self.MKT_SHARE] = topn.iloc[:, 0] / resampled_cap.loc[dt, self.CAPITALIZATION]
        return topn

    def get_top_n_capitalization_companies_for_year(self, n, dt=None):
        """
        Calculates the top-n capitalization companies for the market.

        :param n: the number of maximum capitalization companies to return
        :param dt: (string, int, date, datetime, Timestamp) – the date for whose year to return the topn capitalization
                    if None, the current year is used
        :returns: a pd.Series object capturing the top-n capitalization companies for the market along with the
                  percentage share of their capitalization in the market. The pd.Series is indexed by the ticker
                  symbols.
        """
        if dt is None:
            dt = self.data.index[-1]
        dt = YearBegin(0).rollback(dt) if YearBegin(0).rollback(dt) <= self.data.index[-1] else self.data.index[-1]
        resampled_cap = self.capitalization.resample('AS').mean()
        topn = resampled_cap.loc[dt, resampled_cap.columns[2:]].nlargest(n).to_frame(self.CAPITALIZATION)
        topn[self.MKT_SHARE] = topn.iloc[:, 0] / resampled_cap.loc[dt, self.CAPITALIZATION]
        return topn

    def get_daily_trading_value_ds(self, tickers=None):
        if tickers is None:
            daily_turnover = self.capitalization.loc[:, Metrics.TURNOVER].copy()
        else:
            daily_turnover = pd.Series(0., index=self.data.index)
            for ticker in tickers:
                (st, ed) = self.ticker_symbols[ticker]
                daily_turnover += self.data.loc[st:ed, (Metrics.CLOSE, ticker)]\
                    * self.data.loc[st:ed, (Metrics.VOLUME, ticker)]
        return daily_turnover

    def get_daily_trading_value(self, frequency='M', tickers=None):
        """
        Calculates a total daily trading value of stocks in the market implied by this object.
        Downsamples if a less granular frequency than daily is specified. Takes an average turnover over periods
        implied by the 'frequency' parameter.

        :param frequency: a standard Pandas frequency designator
            https://pandas.pydata.org/pandas-docs/stable/user_guide/timeseries.html#timeseries-offset-aliases
        :param tickers: a list of one or more ticker symbols
        :returns: a pd.Series object capturing the turnover
        """
        return self.get_daily_trading_value_ds(tickers).resample(frequency).mean().dropna().rename(self.TOTAL_VALUE)

    def get_annual_trading_value(self, frequency='M', tickers=None):
        return self.get_daily_trading_value(frequency, tickers) * self.TRADING_DAYS_IN_YEAR

    def get_daily_turnover(self, frequency='M', tickers=None):
        """
        Calculates a daily turnover ratio of stocks in the market implied by this object to its capitalization.
        Downsamples if a less granular frequency than daily is specified. Takes an average turnover over periods
        implied by the 'frequency' parameter.

        :param frequency: a standard Pandas frequency designator
            https://pandas.pydata.org/pandas-docs/stable/user_guide/timeseries.html#timeseries-offset-aliases
        :param tickers: a list of one or more ticker symbols
        :returns: a pd.Series object capturing the turnover
        """
        daily_turnover = self.get_daily_trading_value_ds(tickers)
        if tickers is None:
            daily_turnover /= self.capitalization.loc[:, Metrics.CAPITALIZATION]
        else:
            capitalization = pd.Series(0., index=self.data.index)
            for ticker in tickers:
                (st, ed) = self.ticker_symbols[ticker]
                capitalization += self.data.loc[st:ed, (Metrics.CLOSE, ticker)]\
                    * self.shares_outstanding[ticker].loc[st:ed]
            daily_turnover /= capitalization

        return daily_turnover.resample(frequency).mean().dropna()

    def get_annual_turnover(self, frequency='M', tickers=None):
        """
        Calculates an annual turnover ratio of stocks in the market implied by this object to its capitalization.
        Downsamples if a less granular frequency than daily is specified. Takes an average turnover over periods
        implied by the 'frequency' parameter.

        :param frequency: a standard Pandas frequency designator
            https://pandas.pydata.org/pandas-docs/stable/user_guide/timeseries.html#timeseries-offset-aliases
        :param tickers: a list of one or more ticker symbols
        :returns: a pd.Series object capturing the turnover
        """
        return self.get_daily_turnover(frequency, tickers) * self.TRADING_DAYS_IN_YEAR

    def get_annual_volatility(self, alpha=1-.94453, frequency='M'):
        """
        Calculates an annual volatility of a market represented by the stock market index used when constructing
        this instance of Metrics. It uses an exponentially weighted moving average (EWMA) with a given smoothing factor
        alpha. It downsamples it if a less granular frequency than daily is specified. When downsampling it takes
        an average volatility over periods implied by the 'frequency' parameter.

        :param alpha: a smoothing factor alpha. Then calculating the EWMA, the most recent observation will be
                      multiplied by 'alpha', while the previous estimate of EMWA by '(1-alpha)'
        :param frequency: a standard Pandas frequency designator
            https://pandas.pydata.org/pandas-docs/stable/user_guide/timeseries.html#timeseries-offset-aliases
        :returns: a pd.Series object capturing the volatility
        """
        if self.stock_index_data is None:
            return None
        vol = self.stock_index_data.pct_change().ewm(alpha=alpha).std() * self.TO_ANNUAL_MULTIPLIER
        return vol.resample(frequency).mean().dropna().rename(self.VOLATILITY)

    def get_current_components(self):
        """
        Returns the current set of components in a given stock market.
        """
        return [k for k, (st, ed) in self.ticker_symbols.items() if ed is None]


class USStockMarketMetrics(Metrics):
    def __init__(self, tickers, stock_index='^GSPC', start=None, hist_shares_outs=None):
        """
        Constructs a Metrics object out of a list of ticker symbols and an optional index ticker

        :param tickers: a dictionary representing all the ticker symbols making up a stock market that this class
                        derives metrics for. Each key represents a ticker symbol, each value the start and end day of
                        the stock represented by the ticker being added or removed. An end value of None implies it's
                        still part of the market, a start value of None designates it becoming part of the market
                        before 'start'
        :param stock_index: an optional ticker symbol of the index that corresponds to the market represented
                            by the 'tickers' parameter
        :param start: (string, int, date, datetime, Timestamp) – Starting date. Parses many kinds of date
                       representations (e.g., ‘JAN-01-2010’, ‘1/1/10’, ‘Jan, 1, 1980’). Defaults to 5 years before
                       current date.
        :param hist_shares_outs: a dictionary representing historical shares outstanding for delisted tickers.
                            Each key represents a ticker symbol. Each value is a panda Series designating shares
                            outstanding on certain days.
        """
        super().__init__(tickers, stock_index, start, hist_shares_outs)

    @staticmethod
    def get_sp500_components():
        """
        Returns the current constituent components of the S&P 500 Stock Index. Given that three corporations in
        the index have class B shares, the method returns 503 ticker symbols
        """
        table = pd.read_html('https://en.wikipedia.org/wiki/List_of_S%26P_500_companies')
        df = table[0]
        # Correction for Yahoo-Finance's representation of Class B shares
        sp500_components = [ticker.replace('.', '-') for ticker in df['Symbol'].to_list()]
        return sp500_components

    @staticmethod
    def get_sp500_historical_components(start=None):
        """
        Returns a dictionary whose keys are ticker symbols representing companies that were part of the S&P 500 Index
        at any time since 'start' and whose values are pairs representing the dates of inclusion and exclusion from
        the index. A start date of 'None' means the ticker was part of the index before 'start'. An end date of 'None'
        implies the ticker is still part of the index.

        :param start: an int, float, str, datetime, or date object designating the starting time for calculating the
                      history of the constituent components of the S&P 500 Index.
        """
        if type(start) is date:
            start = datetime.combine(start, time())
        start = pd.to_datetime(start)
        all_components = USStockMarketMetrics.get_sp500_components().copy()
        ret = {ticker: (start, None) for ticker in all_components}
        # current_tickers = frozenset(all_components)
        # vals = []

        df = pd.read_csv('./stock_market/sp500_changes_since_2019.csv', index_col=[0])
        for idx, row in df[::-1].iterrows():
            ts = pd.to_datetime(idx)
            if ts < start:
                break
            for added_ticker in [] if pd.isnull(row[0]) else row[0].split(','):
                _, end = ret[added_ticker]
                ret[added_ticker] = (ts, end)

            for removed_ticker in row[1].split(','):
                ret[removed_ticker] = (start, ts-BDay(1))
                all_components.append(removed_ticker)

            # vals = [(pd.to_datetime(idx), current_tickers)]
            # current_tickers -= frozenset(row[0].split(','))
            # current_tickers |= frozenset(row[1].split(','))
            # print(f'On {pd.to_datetime(idx):%Y-%m-%d} the index had {len(vals[-1][1]):d} components')
            # assert current_tickers != vals[-1][1], "not different"
            # assert 500 <= len(current_tickers) <= 505, "wrong length"

        # vals.append((BYearBegin(1).rollback(vals[-1][0]), current_tickers))
        # print(vals)
        if len(all_components) > len(ret):
            raise ValueError('Some tickers were added twice during the implied period')

        return ret

    @staticmethod
    def get_sp500_historical_shares_outstanding():
        """
        Returns a dictionary whose keys are ticker symbols representing companies that were part of the S&P 500 Index
        at any time since of 2020 and whose values are pd.Series objects representing the number of shares outstanding
        on a given reporting date. I obtained the numbers by going through the quarterly and annual reports of the
        respective companies.
        """
        last_bd = BDay(0).rollback
        return {'AGN': pd.Series([329002015, 329805791],
                                 index=pd.DatetimeIndex(['2020-02-12', '2020-05-01']).map(last_bd)),
                'ALXN': pd.Series([218845432, 219847960, 221019230],
                                  index=pd.DatetimeIndex(['2020-10-27', '2021-02-12', '2021-04-21']).map(last_bd)),
                'ARNC': pd.Series([109021376, 109052877],
                                  index=pd.DatetimeIndex(['2020-03-19', '2020-05-15']).map(last_bd)),
                'ATVI': pd.Series([772857185, 777016759, 782306592, 782625319, 784274126, 786158727, 786798320],
                                  index=pd.DatetimeIndex(['2020-10-22', '2021-04-27', '2022-07-25', '2022-10-31',
                                                          '2023-02-16', '2023-04-28', '2023-07-24']).map(last_bd)),
                'CERN': pd.Series([306589898, 301317068, 294222760, 294098094],
                                  index=pd.DatetimeIndex(['2020-10-21', '2021-04-30', '2021-10-25',
                                                          '2022-04-26']).map(last_bd)),
                'CTXS': pd.Series([123123572, 124167045, 124230000, 124722872, 126579926, 126885081],
                                  index=pd.DatetimeIndex(['2020-10-23', '2021-04-29', '2021-06-30', '2021-11-01',
                                                          '2022-04-27', '2022-07-18']).map(last_bd)),
                'CXO': pd.Series([196707339, 196304640],
                                 index=pd.DatetimeIndex(['2020-07-26', '2020-10-23']).map(last_bd)),
                'DISCA': pd.Series([158566403, 160019717, 160205701, 160318208, 162490752, 169207249, 169580151],
                                   index=pd.DatetimeIndex(['2020-02-13', '2020-04-22', '2020-07-24', '2020-10-26',
                                                           '2021-02-08','2021-10-22', '2022-02-10']).map(last_bd)),
                'DISCK': pd.Series([355843540, 340161506, 340170764, 324172931, 318331065, 330146263, 330153753],
                                   index=pd.DatetimeIndex(['2020-02-13', '2020-04-22', '2020-07-24', '2020-10-26',
                                                           '2021-02-08', '2021-10-22', '2022-02-10']).map(last_bd)),
                'DISH': pd.Series([287530751, 288909818, 289454037, 290571195, 291559614, 292270989, 294172528],
                                  index=pd.DatetimeIndex(['2020-10-22', '2021-04-19', '2021-07-26', '2022-02-14',
                                                          '2022-04-25', '2022-10-24', '2023-04-25']).map(last_bd)),
                'DRE': pd.Series([368382161, 370561785, 371951171, 374985270, 378340411, 380850300, 382767539,
                                  384455127, 384992716],
                                 index=pd.DatetimeIndex(['2020-04-29', '2020-07-29', '2020-10-27', '2021-04-28',
                                                         '2021-07-28', '2021-10-27', '2022-02-16', '2022-04-27',
                                                         '2022-08-04']).map(last_bd)),
                'ETFC': pd.Series([221046419, 221096380],
                                  index=pd.DatetimeIndex(['2020-04-30', '2020-08-03']).map(last_bd)),
                'FLIR': pd.Series([130842358, 131121965, 131144505, 131238445, 131932461],
                                  index=pd.DatetimeIndex(['2020-05-01', '2020-07-31', '2020-10-23',
                                                          '2021-02-19', '2021-04-30']).map(last_bd)),
                'INFO': pd.Series([396809671, 398612292, 398841378, 399080370],
                                  index=pd.DatetimeIndex(['2020-05-31', '2021-05-31', '2021-08-31',
                                                          '2021-12-31']).map(last_bd)),
                'KSU': pd.Series([90964664, 90980440],
                                 index=pd.DatetimeIndex(['2021-07-09', '2021-10-12']).map(last_bd)),
                'MXIM': pd.Series([266625382, 266695209, 267301195, 268363654, 268566248],
                                  index=pd.DatetimeIndex(['2020-04-17', '2020-08-10', '2020-10-15',
                                                          '2021-04-16', '2021-08-10']).map(last_bd)),
                'NBL': pd.Series([479698676, 479768764],
                                 index=pd.DatetimeIndex(['2020-03-31', '2020-06-30']).map(last_bd)),
                'NLSN': pd.Series([356475591, 359941875],
                                  index=pd.DatetimeIndex(['2020-03-31', '2022-09-30']).map(last_bd)),
                'PBCT': pd.Series([424777066, 428020009],
                                  index=pd.DatetimeIndex(['2020-07-31', '2021-10-31']).map(last_bd)),
                'RTN': pd.Series([278479000, 278441000],
                                 index=pd.DatetimeIndex(['2019-10-21', '2020-02-10']).map(last_bd)),
                'SIVB': pd.Series([51796902, 58687392, 59104124],
                                  index=pd.DatetimeIndex(['2020-10-31', '2021-10-31', '2022-10-31']).map(last_bd)),
                'TIF': pd.Series([121368585, 121411166],
                                 index=pd.DatetimeIndex(['2020-07-31', '2020-10-31']).map(last_bd)),
                'TWTR': pd.Series([795349591, 798152488, 798126631, 799609869, 800641166, 764180688, 765246152],
                                  index=pd.DatetimeIndex(['2020-10-29', '2021-02-09', '2021-04-23', '2021-10-22',
                                                          '2022-02-10', '2022-04-22', '2022-07-22']).map(last_bd)),
                'VAR': pd.Series([91355469, 91838813],
                                 index=pd.DatetimeIndex(['2020-11-13', '2021-01-29']).map(last_bd)),
                'WCG': pd.Series([50312077, 50327612],
                                 index=pd.DatetimeIndex(['2019-07-26', '2019-10-28']).map(last_bd)),
                'XEC': pd.Series([101810140, 102135577],
                                 index=pd.DatetimeIndex(['2019-10-31', '2020-01-31']).map(last_bd)),
                'XLNX': pd.Series([243846000, 244314000, 245277000, 247468170, 247880415, 248382008],
                                  index=pd.DatetimeIndex(['2020-04-24', '2020-07-10', '2021-01-15', '2021-07-16',
                                                          '2021-10-15', '2022-01-14']).map(last_bd))}


if __name__ == "__main__":
    import sys
    import locale
    import traceback

    try:
        locale.setlocale(locale.LC_ALL, '')
        start = date(2020, 1, 1)
        end = date.today()

        sp500_hist_comps = USStockMarketMetrics.get_sp500_historical_components(start)
        sp500_hist_shares_outs = USStockMarketMetrics.get_sp500_historical_shares_outstanding()
        sp500_metrics = USStockMarketMetrics(sp500_hist_comps, start=start, hist_shares_outs=sp500_hist_shares_outs)

        print(sp500_metrics.get_top_n_capitalization_companies_for_day(10))
        print(sp500_metrics.get_top_n_capitalization_companies_for_month(10, end))
        print(sp500_metrics.get_capitalization().tail(32))
        print(sp500_metrics.get_daily_turnover().tail(32))
        print(sp500_metrics.get_annual_turnover().tail(10))
        print(sp500_metrics.get_annual_volatility().tail(10))
        print(f'S&P 500 forward dividend yield on {sp500_metrics.data.index[-1]:%Y-%m-%d} is '
              f'{sp500_metrics.forward_dividend_yield:.3%}')

    except Exception as ex:
        traceback.print_exception(type(ex), ex, ex.__traceback__)
        print("Unexpected error: ", sys.exc_info())
