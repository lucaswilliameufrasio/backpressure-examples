import Config

config :backpressure_elixir, serve_http: true

import_config "#{config_env()}.exs"
