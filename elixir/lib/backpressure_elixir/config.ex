defmodule Backpressure.Elixir.Config do
  @moduledoc false

  @defaults %{
    queue_capacity: 32,
    workers: 4,
    downstream_concurrency: 2,
    tenant_outstanding_limit: 0,
    process_delay_ms: 750,
    job_timeout_ms: 5_000,
    rate_per_second: 10,
    rate_window_ms: 1_000,
    max_retries: 3,
    retry_base_ms: 25,
    shutdown_timeout_ms: 10_000,
    port: 3003
  }

  def load do
    Map.new(@defaults, fn {key, default} ->
      {key, read_setting(key, default)}
    end)
    |> Map.put(:serve_http, Application.get_env(:backpressure_elixir, :serve_http, true))
  end

  def current do
    Application.get_env(:backpressure_elixir, :runtime_config, load())
  end

  defp read_setting(key, default) do
    env = key |> Atom.to_string() |> Macro.underscore() |> String.upcase()

    case System.get_env(env) do
      nil -> Application.get_env(:backpressure_elixir, key, default)
      value -> parse_setting(value, key, default)
    end
  end

  defp parse_setting(value, key, default) do
    case Integer.parse(value) do
      {number, ""} -> if(number >= minimum(key), do: number, else: default)
      _ -> default
    end
  end

  defp minimum(key)
       when key in [
              :queue_capacity,
              :workers,
              :downstream_concurrency,
              :job_timeout_ms,
              :rate_per_second,
              :rate_window_ms,
              :shutdown_timeout_ms,
              :port
            ],
       do: 1

  defp minimum(_key), do: 0
end
