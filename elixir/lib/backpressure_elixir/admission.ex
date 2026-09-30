defmodule Backpressure.Elixir.Admission do
  @moduledoc false

  alias Backpressure.Elixir.{Config, Metrics, Producer}

  @tenants ~w(alpha beta default)

  def tenants, do: @tenants

  def submit(tenant, failures \\ 0) when tenant in @tenants do
    config = Config.current()
    outstanding_limit = config.queue_capacity + config.workers

    with :ok <- reserve(:outstanding, outstanding_limit),
         :ok <- reserve_tenant(tenant, config.tenant_outstanding_limit) do
      id = Metrics.add(:next_job_id)
      job = %{id: id, tenant: tenant, failures: failures}
      Metrics.add(:jobs_enqueued_total)
      Metrics.add(:queue_depth)
      GenStage.cast(Producer, {:publish, job})
      {:ok, id}
    else
      {:error, reason} ->
        if reason == :tenant_full, do: Metrics.add(:tenant_rejected_total)
        Metrics.add(:jobs_rejected_total)
        {:error, reason}
    end
  end

  def submitted(job) do
    Metrics.add(:queue_depth, -1)
    Metrics.add(:in_flight)
    started_at = System.monotonic_time(:millisecond)
    result = process(job, Config.current())
    duration = max(0, System.monotonic_time(:millisecond) - started_at)
    Metrics.add(:processing_duration_ms_total, duration)
    Metrics.add(:in_flight, -1)
    Metrics.add(:outstanding, -1)
    release_tenant(job.tenant)

    case result do
      :ok -> Metrics.add(:jobs_processed_total)
      {:error, _reason} -> Metrics.add(:jobs_failed_total)
    end
  end

  def try_acquire_downstream do
    Metrics.try_acquire(:downstream_in_use, Config.current().downstream_concurrency)
  end

  def release_downstream, do: Metrics.release(:downstream_in_use)

  def enter_sync do
    case try_acquire_downstream() do
      :ok ->
        Metrics.add(:sync_requests_in_flight)
        :ok

      :full ->
        Metrics.add(:jobs_rejected_total)
        :full
    end
  end

  def leave_sync do
    Metrics.add(:sync_requests_in_flight, -1)
    release_downstream()
  end

  def drain(timeout_ms) do
    deadline = System.monotonic_time(:millisecond) + timeout_ms
    wait_for_drain(deadline)
  end

  defp wait_for_drain(deadline) do
    cond do
      Metrics.get(:outstanding) == 0 ->
        :ok

      System.monotonic_time(:millisecond) >= deadline ->
        :timeout

      true ->
        Process.sleep(10)
        wait_for_drain(deadline)
    end
  end

  defp process(job, config) do
    deadline = System.monotonic_time(:millisecond) + config.job_timeout_ms

    with :ok <- acquire_until(deadline, config.downstream_concurrency),
         result <- attempt(job, 1, deadline, config) do
      release_downstream()
      result
    else
      {:error, :downstream_timeout} = error -> error
    end
  end

  defp acquire_until(deadline, limit) do
    case Metrics.try_acquire(:downstream_in_use, limit) do
      :ok ->
        :ok

      :full ->
        if System.monotonic_time(:millisecond) >= deadline do
          {:error, :downstream_timeout}
        else
          Process.sleep(2)
          acquire_until(deadline, limit)
        end
    end
  end

  defp attempt(job, attempt_number, deadline, config) do
    remaining = deadline - System.monotonic_time(:millisecond)

    if remaining <= 0 do
      {:error, :timeout}
    else
      Process.sleep(min(config.process_delay_ms, remaining))

      cond do
        System.monotonic_time(:millisecond) >= deadline ->
          {:error, :timeout}

        attempt_number <= job.failures and attempt_number <= config.max_retries ->
          Metrics.add(:retries_total)
          backoff = retry_delay(attempt_number, config.retry_base_ms)
          Process.sleep(min(backoff, max(0, deadline - System.monotonic_time(:millisecond))))
          attempt(job, attempt_number + 1, deadline, config)

        attempt_number <= job.failures ->
          {:error, :retry_limit}

        true ->
          :ok
      end
    end
  end

  defp retry_delay(attempt, base_ms) do
    ceiling = min(base_ms * trunc(:math.pow(2, attempt - 1)), 1_000)
    ceiling + :rand.uniform(max(1, div(ceiling, 4)))
  end

  defp reserve(key, limit) do
    if Metrics.add(key) <= limit do
      :ok
    else
      Metrics.add(key, -1)
      {:error, :queue_full}
    end
  end

  defp reserve_tenant(_tenant, 0), do: :ok

  defp reserve_tenant(tenant, limit) do
    case reserve({:tenant_outstanding, tenant}, limit) do
      :ok ->
        :ok

      {:error, _reason} ->
        Metrics.add(:outstanding, -1)
        {:error, :tenant_full}
    end
  end

  defp release_tenant(tenant) do
    if Config.current().tenant_outstanding_limit > 0 do
      Metrics.add({:tenant_outstanding, tenant}, -1)
    end
  end
end
