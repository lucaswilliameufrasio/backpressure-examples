defmodule Backpressure.Elixir.Router do
  @moduledoc false
  use Plug.Router

  alias Backpressure.Elixir.{Admission, Config, Metrics}
  import Plug.Conn

  plug :fetch_query_params
  plug :match
  plug :dispatch

  get "/healthz" do
    json(conn, 200, %{status: "ok"})
  end

  post "/jobs" do
    tenant = Map.get(conn.query_params, "tenant", "default")
    failures = int_param(conn.query_params, "failures", 0, 0, 10)

    cond do
      tenant not in Admission.tenants() ->
        json(conn, 400, %{error: "tenant must be alpha, beta, or default"})

      is_nil(failures) ->
        json(conn, 400, %{error: "failures must be between 0 and 10"})

      true ->
        case Admission.submit(tenant, failures) do
          {:ok, job_id} ->
            json(conn, 202, %{status: "queued", job_id: job_id, tenant: tenant})

          {:error, :tenant_full} ->
            conn |> put_resp_header("retry-after", "1") |> json(429, %{error_code: "TENANT_BUSY"})

          {:error, _reason} ->
            conn |> put_resp_header("retry-after", "1") |> json(429, %{error_code: "QUEUE_FULL"})
        end
    end
  end

  get "/sync" do
    case Admission.enter_sync() do
      :ok ->
        try do
          Process.sleep(Config.current().process_delay_ms)
          json(conn, 200, %{status: "processed"})
        after
          Admission.leave_sync()
        end

      :full ->
        json(conn, 503, %{error_code: "DOWNSTREAM_SATURATED"})
    end
  end

  get "/limited" do
    config = Config.current()

    if Metrics.allow_rate?(config.rate_per_second, config.rate_window_ms) do
      json(conn, 200, %{status: "allowed"})
    else
      conn
      |> put_resp_header("retry-after", "1")
      |> json(429, %{error_code: "RATE_LIMITED"})
    end
  end

  get "/batch" do
    config = Config.current()
    items = int_param(conn.query_params, "items", 100, 1, 2_000)
    concurrency = int_param(conn.query_params, "concurrency", 8, 1, 64)

    if is_nil(items) or is_nil(concurrency) do
      json(conn, 400, %{error: "items must be 1..2000 and concurrency 1..64"})
    else
      results =
        1..items
        |> Task.async_stream(
          fn _item -> Process.sleep(config.process_delay_ms) end,
          max_concurrency: concurrency,
          timeout: config.job_timeout_ms,
          on_timeout: :kill_task,
          ordered: false
        )
        |> Enum.to_list()

      if Enum.all?(results, &match?({:ok, :ok}, &1)) do
        json(conn, 200, %{items: items, completed: items, concurrency: concurrency})
      else
        json(conn, 504, %{error_code: "BATCH_TIMEOUT"})
      end
    end
  end

  get "/cpu" do
    millis = int_param(conn.query_params, "ms", 50, 1, 1_000)

    if is_nil(millis) do
      json(conn, 400, %{error: "ms must be between 1 and 1000"})
    else
      case Admission.enter_sync() do
        :ok ->
          try do
            checksum = burn_cpu(System.monotonic_time(:millisecond) + millis, 0, 0)
            json(conn, 200, %{status: "computed", cpu_ms: millis, checksum: checksum})
          after
            Admission.leave_sync()
          end

        :full ->
          json(conn, 503, %{error_code: "DOWNSTREAM_SATURATED"})
      end
    end
  end

  get "/stream" do
    items = int_param(conn.query_params, "items", 20, 1, 1_000)
    delay = int_param(conn.query_params, "delay_ms", 10, 0, 1_000)

    if is_nil(items) or is_nil(delay) do
      json(conn, 400, %{error: "items must be 1..1000 and delay_ms 0..1000"})
    else
      conn = conn |> put_resp_content_type("text/plain") |> send_chunked(200)

      Enum.reduce_while(1..items, conn, fn item, stream_conn ->
        if delay > 0, do: Process.sleep(delay)

        case chunk(stream_conn, "#{item}\n") do
          {:ok, next_conn} -> {:cont, next_conn}
          {:error, _reason} -> {:halt, stream_conn}
        end
      end)
    end
  end

  get "/metrics" do
    json(conn, 200, Metrics.snapshot(Config.current()))
  end

  match _ do
    json(conn, 404, %{error_code: "NOT_FOUND"})
  end

  defp json(conn, status, body) do
    conn
    |> put_resp_content_type("application/json")
    |> send_resp(status, Jason.encode!(body))
  end

  defp int_param(params, name, default, min, max) do
    case Map.get(params, name) do
      nil ->
        default

      value ->
        case Integer.parse(value) do
          {integer, ""} when integer >= min and integer <= max -> integer
          _ -> nil
        end
    end
  end

  defp burn_cpu(deadline, checksum, iterations) do
    if System.monotonic_time(:millisecond) >= deadline do
      :erlang.phash2(checksum)
    else
      checksum = :erlang.phash2(checksum + iterations + 1)
      burn_cpu(deadline, checksum, iterations + 1)
    end
  end
end
