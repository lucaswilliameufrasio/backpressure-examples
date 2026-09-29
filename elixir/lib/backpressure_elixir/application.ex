defmodule Backpressure.Elixir.Application do
  @moduledoc false
  use Application

  alias Backpressure.Elixir.{Config, Consumer, Metrics, Producer, Router}

  @impl true
  def start(_type, _args) do
    config = Config.load()
    Application.put_env(:backpressure_elixir, :runtime_config, config)

    producer_capacity = config.queue_capacity + config.workers

    children =
      [
        {Metrics, []},
        {Producer, name: Producer, buffer_size: producer_capacity}
      ] ++
        consumers(config) ++
        http_server(config)

    Supervisor.start_link(children,
      strategy: :rest_for_one,
      name: Backpressure.Elixir.Supervisor
    )
  end

  @impl true
  def stop(_state) do
    config = Config.current()
    Backpressure.Elixir.Admission.drain(config.shutdown_timeout_ms)
    :ok
  end

  defp consumers(config) do
    for worker_id <- 1..config.workers do
      Supervisor.child_spec(
        {Consumer, producer: Producer, worker_id: worker_id, config: config},
        id: {:consumer, worker_id}
      )
    end
  end

  defp http_server(%{serve_http: false}), do: []

  defp http_server(config) do
    [
      {Bandit,
       plug: Router, scheme: :http, ip: {127, 0, 0, 1}, port: config.port, startup_log: false}
    ]
  end
end
