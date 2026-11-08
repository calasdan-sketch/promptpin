## promptpin review

1 pin change(s) compared with `HEAD`.

### `production` / `greeter`: `v1` (`975b22d`) to `v2` (`92ffb03`)
- Wording changed: 1 line(s) added, 1 removed.
- Settings changed: params.temperature.
- The model will behave differently even if the words are the same.

| Setting | Before | After |
|---|---|---|
| `params.temperature` | `0.7` | `0.4` |

```diff
--- greeter@975b22d
+++ greeter@92ffb03
@@ -1,2 +1,2 @@
-Greet {{user_name}} warmly in one short sentence.
+Greet {{user_name}} kindly in one short sentence.
 
```

To undo: `promptpin deploy greeter@975b22d115645cde50a89d1c7caa48ac5fd7c72f --env production` (pins the previous commit again).
