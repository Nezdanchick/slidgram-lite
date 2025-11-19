modules_enabled = {
  -- [...]
  -- "http_file_share"; -- for attachments with the "upload" option
  "http_files"; -- for attachments with the "no upload" option
  "privilege"; -- for roster sync and 'legacy carbons'
}

-- for attachments with the "no upload" option
-- in slidge's config: no-upload-path=/var/lib/slidge/attachments
http_files_dir = "/var/lib/slidge/attachments"

local _privileges = {
    roster = "both";
    message = "outgoing";
    iq = {
      ["http://jabber.org/protocol/pubsub"] = "both";
      ["http://jabber.org/protocol/pubsub#owner"] = "set";
    };
}

VirtualHost "example.org"
  -- for roster sync and 'legacy carbons'
  privileged_entities = {
    ["telegram.example.org"] =_privileges,
    ["other-walled-garden.example.org"] = _privileges,
    -- repeat for other slidge plugins…
  }

Component "telegram.example.org"
  component_secret = "secret"
  modules_enabled = {"privilege"}

Component "other-walled-garden.example.org"
  component_secret = "some-other-secret"
  modules_enabled = {"privilege"}

-- …repeat for other slidge-based gateways

-- -- for attachments with the "upload" option
-- -- in telegram's config: upload-service=upload.example.org
-- Component "upload.example.org" "http_file_share"
--     -- max file size: 16 MiB
--     http_file_share_size_limit = 16*1024*1024
--
--     -- max per day per telegram component: 100 MiB
--     http_file_share_daily_quota = 100*1024*1024
--
--     -- 1 GiB total
--     http_file_share_global_quota = 1024*1024*1024
--
--     -- allow slidgram to use the upload service
--     server_user_role = "prosody:registered"
--     -- alternatively, you can be more specific with:
--     -- http_file_share_access = { "telegram.example.org" }
