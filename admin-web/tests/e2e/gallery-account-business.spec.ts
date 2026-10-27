import {expect,test,type BrowserContext,type Route} from "@playwright/test";
const origin=process.env.PLAYWRIGHT_BASE_URL||"http://127.0.0.1:13000";
const user={id:23,username:"reader",display_name:"Reader",is_admin:false,is_active:true,permissions:["library"],nsfw_visible:false,upload_quota_bytes:524288,upload_used_bytes:1024,must_change_password:false,created_at:"2026-10-24T00:00:00Z"};
const me={...user,id:17,username:"account-admin",display_name:"Account Admin",is_admin:true,modules:{library:"Library",system:"System",tasks:"Tasks"},preferences:{lang:"en",theme:"light"}};
const json=(route:Route,body:unknown,status=200)=>route.fulfill({status,contentType:"application/json",body:JSON.stringify(body)});
async function setup(context:BrowserContext,extra?:(route:Route,path:string)=>Promise<boolean>,principal=me){
 const calls:Array<{path:string;method:string;body:any}>=[];
 await context.addCookies([{name:"ag_session",value:"account-fixture",url:origin},{name:"ag_csrf",value:"account-fixture-csrf",url:origin}]);
 await context.addInitScript(()=>{localStorage.setItem("auto-gallery-lang","en");localStorage.setItem("auto-gallery-theme","light");});
 await context.route("**/api/v1/**",async route=>{const path=new URL(route.request().url()).pathname,method=route.request().method();calls.push({path,method,body:route.request().postDataJSON()});
  if(extra&&await extra(route,path))return;
  if(path==="/api/v1/auth/me")return void await json(route,principal);
  if(path==="/api/v1/notifications")return void await json(route,{items:[],total:0});
  if(path==="/api/v1/auth/ws-ticket")return void await json(route,{detail:"fixture websocket unavailable"},503);
  if(path==="/api/v1/users"&&method==="GET")return void await json(route,[me,user]);
  if(path==="/api/v1/users/23"&&method==="GET")return void await json(route,user);
  return void await json(route,{});
 });return calls;
}
test.describe.configure({timeout:60000});test.use({actionTimeout:10000});
test("account creation labels, keyboard submit, administrator permissions, errors and recovery",async({page,context})=>{
 let creates=0;const calls=await setup(context,async(route,path)=>{if(path==="/api/v1/users"&&route.request().method()==="POST"){creates++;await json(route,creates===1?{detail:"Username already exists"}:{...user,id:24},creates===1?409:201);return true;}return false;});
 await page.goto("/admin/settings/users");await page.getByRole("button",{name:"+ New",exact:true}).click();const dialog=page.getByRole("dialog");
 await expect(dialog.getByLabel("Username *",{exact:true})).toBeVisible();await dialog.getByLabel("Username *",{exact:true}).fill("new-reader");await dialog.getByLabel("Initial Password *",{exact:true}).fill("temporary123");
 await dialog.getByRole("checkbox",{name:"Library Browsing",exact:true}).check();await dialog.getByRole("checkbox",{name:"Administrator",exact:true}).check();await expect(dialog.getByRole("checkbox",{name:"Library Browsing",exact:true})).toBeDisabled();
 await dialog.getByLabel("Username *",{exact:true}).press("Enter");await expect(dialog.getByRole("alert")).toContainText("Username already exists");await dialog.getByRole("button",{name:"Create",exact:true}).click();await expect(dialog).toHaveCount(0);
 expect(calls.filter(c=>c.path==="/api/v1/users"&&c.method==="POST").map(c=>c.body)).toEqual([{username:"new-reader",password:"temporary123",is_admin:true,permissions:[]},{username:"new-reader",password:"temporary123",is_admin:true,permissions:[]}]);
});
test("account detail preserves decimal quota and unrelated drafts while saving permissions",async({page,context})=>{
 let current={...user};const calls=await setup(context,async(route,path)=>{if(path!=="/api/v1/users/23")return false;if(route.request().method()==="PATCH")current={...current,...route.request().postDataJSON()};await json(route,current);return true;});
 await page.goto("/admin/settings/users/23");await expect(page.locator("#user-upload-quota")).toHaveValue("0.5");await page.locator("#user-display-name").fill("Reader draft");
 await page.getByRole("checkbox",{name:"System & Settings",exact:true}).check();await expect.poll(()=>current.permissions).toEqual(["library","system"]);await expect(page.locator("#user-display-name")).toHaveValue("Reader draft");
 await page.locator("#user-upload-quota").fill("0.75");await page.locator("#user-upload-quota").locator("..").getByRole("button",{name:"Save",exact:true}).click();await expect.poll(()=>current.upload_quota_bytes).toBe(786432);await expect(page.locator("#user-upload-quota")).toHaveValue("0.75");
 const active=page.getByRole("checkbox",{name:"Account Active",exact:true});await active.click();await expect(active).not.toBeChecked();await expect.poll(()=>current.is_active).toBe(false);
 expect(calls.some(c=>c.method==="PATCH"&&c.body.upload_quota_bytes===786432)).toBe(true);
});
test("account password reset retains confirmation after failure and reveals a closable result",async({page,context})=>{
 let resets=0;await setup(context,async(route,path)=>{if(path==="/api/v1/users/23/reset-password"&&route.request().method()==="POST"){resets++;await json(route,resets===1?{detail:"Reset service unavailable"}:{password:"temporary-reset-password"},resets===1?503:200);return true;}return false;});
 await page.goto("/admin/settings/users/23");await page.getByRole("button",{name:"Reset Password",exact:true}).click();let dialog=page.getByRole("dialog");await dialog.getByRole("button",{name:"Confirm",exact:true}).click();await expect(dialog.getByRole("alert")).toContainText("Reset service unavailable");await dialog.getByRole("button",{name:"Confirm",exact:true}).click();await expect(page.getByRole("dialog")).toContainText("temporary-reset-password");await page.keyboard.press("Escape");await expect(page.getByRole("dialog")).toHaveCount(0);expect(resets).toBe(2);
});
test("profile change password reports mismatch and server errors before a successful retry",async({page,context})=>{
 let submissions=0;const calls=await setup(context,async(route,path)=>{if(path==="/api/v1/auth/browser/change-password"&&route.request().method()==="POST"){submissions++;await json(route,submissions===1?{detail:"Current password is incorrect"}:{ok:true,must_change_password:false},submissions===1?400:200);return true;}return false;});
 await page.goto("/admin/profile");await page.locator("#current-password").fill("current123");await page.locator("#new-password").fill("replacement123");await page.locator("#confirm-password").fill("different123");await page.locator("button[type=submit]").click();await expect(page.locator("#profile-password-error")).toContainText("Passwords do not match");expect(submissions).toBe(0);
 await page.locator("#confirm-password").fill("replacement123");await page.locator("#confirm-password").press("Enter");await expect(page.locator("#profile-password-error")).toContainText("Current password is incorrect");await page.locator("button[type=submit]").click();await expect(page.locator("#current-password")).toHaveValue("");await expect(page.locator("#new-password")).toHaveValue("");expect(calls.filter(c=>c.path==="/api/v1/auth/browser/change-password").map(c=>c.body)).toEqual(Array(2).fill({current_password:"current123",new_password:"replacement123"}));
});
test("non-admin account cannot load users APIs but can access its own profile",async({page,context})=>{
 const calls=await setup(context,undefined,{...me,is_admin:false,permissions:["library"]});await page.goto("/admin/settings/users");await expect(page.getByText("You don't have permission to access this page",{exact:true})).toBeVisible();expect(calls.filter(c=>c.path==="/api/v1/users")).toEqual([]);await page.goto("/admin/profile");await expect(page.locator("#current-password")).toBeVisible();
});

test("account permission changes respond immediately and roll back after a rejected update",async({page,context})=>{
 let release:(()=>void)|undefined;await setup(context,async(route,path)=>{if(path==="/api/v1/users/23"&&route.request().method()==="PATCH"){await new Promise<void>(resolve=>{release=resolve;});await json(route,{detail:"Permission update rejected"},403);return true;}return false;});
 await page.goto("/admin/settings/users/23");const control=page.getByRole("checkbox",{name:"System & Settings",exact:true});await control.check();await expect(control).toBeChecked();await expect(control).toBeDisabled();release!();await expect(control).not.toBeChecked();await expect(control).toBeEnabled();await expect(page.getByRole("log")).toContainText("Permission update rejected");
});

test("appearance keeps a manual dark choice and light restores the gallery canvas",async({page,context})=>{
 await setup(context);await page.goto("/admin/settings/appearance");const panel=page.locator(".gallery-settings-page");await panel.getByRole("button",{name:"Dark",exact:true}).click();await expect(page.locator("html")).toHaveClass(/dark/);await expect.poll(()=>page.evaluate(()=>localStorage.getItem("auto-gallery-theme"))).toBe("dark");await panel.getByRole("button",{name:"Light",exact:true}).click();await expect(page.locator("html")).not.toHaveClass(/dark/);await expect.poll(()=>page.locator(".gallery-ui").evaluate(e=>getComputedStyle(e).getPropertyValue("--canvas").trim())).toBe("#edf1ed");
});

test("failed account name and quota saves preserve drafts for retry",async({page,context})=>{
 let current={...user};const attempts:Record<string,number>={};await setup(context,async(route,path)=>{if(path!=="/api/v1/users/23")return false;if(route.request().method()==="PATCH"){const changes=route.request().postDataJSON(),key=Object.keys(changes)[0];attempts[key]=(attempts[key]||0)+1;if(attempts[key]===1){await json(route,{detail:"Save temporarily unavailable"},503);return true;}current={...current,...changes};}await json(route,current);return true;});
 await page.goto("/admin/settings/users/23");for(const [id,value,key]of [["#user-display-name","Reader revised","display_name"],["#user-upload-quota","0.75","upload_quota_bytes"]]){const input=page.locator(id),save=input.locator("..").getByRole("button",{name:"Save",exact:true});await input.fill(value);await save.click();await expect.poll(()=>attempts[key]).toBe(1);await expect(save).toBeEnabled();await expect(input).toHaveValue(value);await save.click();await expect.poll(()=>attempts[key]).toBe(2);await expect(save).toBeDisabled();await expect(input).toHaveValue(value);}
 expect(current.display_name).toBe("Reader revised");expect(current.upload_quota_bytes).toBe(786432);
});
