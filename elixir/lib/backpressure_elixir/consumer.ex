defmodule Backpressure.Elixir.Consumer do
  @moduledoc false
  use GenStage

  alias Backpressure.Elixir.Admission

  def start_link(opts), do: GenStage.start_link(__MODULE__, opts)

  @impl true
  def init(opts) do
    {:consumer, opts,
     subscribe_to: [{Keyword.fetch!(opts, :producer), max_demand: 2, min_demand: 1}]}
  end

  @impl true
  def handle_events(jobs, _from, state) do
    Enum.each(jobs, &Admission.submitted/1)
    {:noreply, [], state}
  end
end
