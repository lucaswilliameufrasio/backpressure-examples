defmodule Backpressure.Elixir.Metrics do
  @moduledoc false
  use GenServer

  @table :backpressure_elixir_metrics
  @initial_counters [
    outstanding: 0,
    queue_depth: 0,
    in_flight: 0,
    downstream_in_use: 0,
    sync_requests_in_flight: 0,
    jobs_enqueued_total: 0,
    jobs_processed_total: 0,
    jobs_failed_total: 0,
    jobs_rejected_total: 0,
    tenant_rejected_total: 0,
    retries_total: 0,
    rate_limited_total: 0,
    processing_duration_ms_total: 0,
    next_job_id: 0
  ]

  def start_link(_opts), do: GenServer.start_link(__MODULE__, :ok, name: __MODULE__)

  @impl true
  def init(:ok) do
    :ets.new(@table, [
      :named_table,
      :public,
      :set,
      read_concurrency: true,
      write_concurrency: true
    ])

    :ets.insert(@table, @initial_counters)
    Process.send_after(self(), :clean_rate_windows, 60_000)
    {:ok, :ok}
  end

  def table, do: @table

  def get(key, default \\ 0) do
    case :ets.lookup(@table, key) do
      [{^key, value}] -> value
      [] -> default
    end
  end

  def add(key, increment \\ 1) do
    :ets.update_counter(@table, key, {2, increment}, {key, 0})
  end

  def try_acquire(key, limit) when limit > 0 do
    if add(key) <= limit do
      :ok
    else
      add(key, -1)
      :full
    end
  end

  def release(key), do: add(key, -1)

  def allow_rate?(limit, window_ms) do
    window = div(System.monotonic_time(:millisecond), window_ms)
    key = {:rate_window, window}
    count = :ets.update_counter(@table, key, {2, 1}, {key, 0})

    if count <= limit do
      true
    else
      add(:rate_limited_total)
      false
    end
  end

  def snapshot(config) do
    Map.new(@initial_counters, fn {key, _initial} -> {key, get(key)} end)
    |> Map.drop([:outstanding, :next_job_id])
    |> Map.merge(%{
      queue_capacity: config.queue_capacity,
      max_outstanding: config.queue_capacity + config.workers,
      workers: config.workers,
      jobs_in_flight: get(:in_flight),
      downstream_concurrency: config.downstream_concurrency,
      downstream_in_use: get(:downstream_in_use),
      tenant_outstanding_limit: config.tenant_outstanding_limit
    })
  end

  @impl true
  def handle_info(:clean_rate_windows, state) do
    current_window = div(System.monotonic_time(:millisecond), 1_000)

    @table
    |> :ets.tab2list()
    |> Enum.each(fn
      {{:rate_window, window}, _count} when window < current_window - 1 ->
        :ets.delete(@table, {:rate_window, window})

      _other ->
        :ok
    end)

    Process.send_after(self(), :clean_rate_windows, 60_000)
    {:noreply, state}
  end
end
