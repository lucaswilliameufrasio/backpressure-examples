defmodule Backpressure.ElixirTest do
  use ExUnit.Case, async: false

  alias Backpressure.Elixir.{Admission, Config, Metrics, Router}

  setup do
    assert :ok == Admission.drain(3_000)
    :ok
  end

  test "bounded admission rejects after outstanding capacity is reached" do
    assert {:ok, _first_id} = Admission.submit("default")
    assert {:ok, _second_id} = Admission.submit("default")
    assert {:error, :queue_full} = Admission.submit("default")
    assert Metrics.get(:jobs_rejected_total) >= 1
  end

  test "tenant bulkhead rejects one tenant but admits another" do
    config = Config.current()

    Application.put_env(:backpressure_elixir, :runtime_config, %{
      config
      | tenant_outstanding_limit: 1
    })

    on_exit(fn -> Application.put_env(:backpressure_elixir, :runtime_config, config) end)

    assert {:ok, _first_id} = Admission.submit("alpha")
    assert {:error, :tenant_full} = Admission.submit("alpha")
    assert {:ok, _other_tenant_id} = Admission.submit("beta")
    assert Metrics.get(:tenant_rejected_total) >= 1
  end

  test "downstream semaphore fails fast when saturated" do
    assert :ok = Admission.enter_sync()
    assert :full = Admission.enter_sync()
    Admission.leave_sync()
    assert Metrics.get(:downstream_in_use) == 0
  end

  test "rate limiter permits its window quota and rejects the next request" do
    limit = Config.current().rate_per_second
    window = Config.current().rate_window_ms
    assert Metrics.allow_rate?(limit, window)
    refute Metrics.allow_rate?(limit, window)
  end

  test "jobs retry transient failures and eventually complete" do
    retries_before = Metrics.get(:retries_total)
    processed_before = Metrics.get(:jobs_processed_total)
    assert {:ok, _job_id} = Admission.submit("default", 2)
    assert :ok == Admission.drain(3_000)
    assert Metrics.get(:retries_total) - retries_before == 2
    assert Metrics.get(:jobs_processed_total) - processed_before == 1
  end

  test "HTTP plug exposes the common routes and validates bounded batch input" do
    conn = Plug.Test.conn(:get, "/healthz")
    response = Router.call(conn, Router.init([]))
    assert response.status == 200

    conn = Plug.Test.conn(:get, "/batch?items=2001")
    response = Router.call(conn, Router.init([]))
    assert response.status == 400
  end

  test "stream endpoint validates item bounds" do
    conn = Plug.Test.conn(:get, "/stream?items=1001")
    response = Router.call(conn, Router.init([]))
    assert response.status == 400
  end
end
