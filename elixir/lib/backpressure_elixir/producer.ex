defmodule Backpressure.Elixir.Producer do
  @moduledoc """
  A GenStage producer with an explicit bounded internal event buffer.

  Admission is reserved before a cast is sent to this process, so both the
  producer mailbox and the GenStage buffer are bounded by the same outstanding
  work budget rather than relying on the default 10,000-event buffer.
  """
  use GenStage

  def start_link(opts) do
    GenStage.start_link(__MODULE__, opts, name: Keyword.fetch!(opts, :name))
  end

  @impl true
  def init(opts) do
    {:producer, :ok, buffer_size: Keyword.fetch!(opts, :buffer_size), buffer_keep: :first}
  end

  @impl true
  def handle_demand(_demand, state), do: {:noreply, [], state}

  @impl true
  def handle_cast({:publish, job}, state), do: {:noreply, [job], state}
end
