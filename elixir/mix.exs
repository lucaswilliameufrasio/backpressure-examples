defmodule BackpressureElixir.MixProject do
  use Mix.Project

  def project do
    [
      app: :backpressure_elixir,
      version: "0.1.0",
      elixir: "~> 1.19",
      start_permanent: Mix.env() == :prod,
      deps: deps(),
      aliases: [check: ["format --check-formatted", "test", "compile --warnings-as-errors"]]
    ]
  end

  def application do
    [
      extra_applications: [:logger],
      mod: {Backpressure.Elixir.Application, []}
    ]
  end

  defp deps do
    [
      {:bandit, "~> 1.12.5"},
      {:gen_stage, "~> 1.3.2"},
      {:jason, "~> 1.4"},
      {:plug, "~> 1.20.3"}
    ]
  end
end
