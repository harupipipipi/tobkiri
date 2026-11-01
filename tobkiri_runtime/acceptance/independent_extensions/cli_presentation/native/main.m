#import <Foundation/Foundation.h>
#import <CommonCrypto/CommonDigest.h>
#import <mach-o/dyld.h>
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#ifndef COMMAND_DIGEST
#error immutable command resource digest required
#endif
static const NSUInteger Limit = 1048576;
static NSString * const GenericError = @"CLI Application request denied or unavailable";
static BOOL True(id x) { return [x isKindOfClass:[NSNumber class]] && CFGetTypeID((__bridge CFTypeRef)x) == CFBooleanGetTypeID() && [x boolValue]; }
static BOOL IsDict(id x) { return [x isKindOfClass:[NSDictionary class]]; }
static BOOL IsArray(id x) { return [x isKindOfClass:[NSArray class]]; }
static BOOL IsString(id x) { return [x isKindOfClass:[NSString class]]; }
static BOOL Keys(id x, NSArray *keys) {
    return IsDict(x) && [[NSSet setWithArray:[x allKeys]] isEqual:[NSSet setWithArray:keys]];
}
static BOOL Integer(id x, long long low, long long high) {
    return [x isKindOfClass:[NSNumber class]] && CFGetTypeID((__bridge CFTypeRef)x) != CFBooleanGetTypeID()
        && !CFNumberIsFloatType((__bridge CFNumberRef)x) && [x longLongValue] >= low && [x longLongValue] <= high;
}
static BOOL Match(NSString *s, NSString *pattern) {
    if (!IsString(s)) return NO;
    NSRegularExpression *regex = [NSRegularExpression regularExpressionWithPattern:pattern options:0 error:nil];
    return [regex numberOfMatchesInString:s options:0 range:NSMakeRange(0, s.length)] == 1;
}
static void Space(NSString *s, NSUInteger *i) {
    while (*i < s.length && [[NSCharacterSet whitespaceAndNewlineCharacterSet] characterIsMember:[s characterAtIndex:*i]]) (*i)++;
}
static NSString *StringToken(NSString *s, NSUInteger *i) {
    if (*i >= s.length || [s characterAtIndex:*i] != '"') return nil;
    NSUInteger start = (*i)++;
    while (*i < s.length) {
        unichar c = [s characterAtIndex:(*i)++];
        if (c == '\\') { if (*i >= s.length) return nil; (*i)++; }
        else if (c == '"') {
            NSData *raw = [[s substringWithRange:NSMakeRange(start, *i-start)] dataUsingEncoding:NSUTF8StringEncoding];
            id decoded = [NSJSONSerialization JSONObjectWithData:raw options:NSJSONReadingFragmentsAllowed error:nil];
            return IsString(decoded) ? decoded : nil;
        }
    }
    return nil;
}
// Reject duplicate object keys before Foundation's ordinary JSON parser loses them.
static BOOL Scan(NSString *s, NSUInteger *i, NSUInteger depth) {
    if (depth > 64) return NO;
    Space(s, i); if (*i >= s.length) return NO;
    unichar c = [s characterAtIndex:*i];
    if (c == '"') return StringToken(s, i) != nil;
    if (c == '{' || c == '[') {
        BOOL object = c == '{'; unichar close = object ? '}' : ']'; (*i)++;
        NSMutableSet *seen = [NSMutableSet set];
        Space(s, i); if (*i < s.length && [s characterAtIndex:*i] == close) { (*i)++; return YES; }
        while (*i < s.length) {
            if (object) {
                Space(s, i); NSString *key = StringToken(s, i);
                if (!key || [seen containsObject:key]) return NO; [seen addObject:key];
                Space(s, i); if (*i >= s.length || [s characterAtIndex:(*i)++] != ':') return NO;
            }
            if (!Scan(s, i, depth+1)) return NO;
            Space(s, i); if (*i >= s.length) return NO;
            unichar next = [s characterAtIndex:(*i)++];
            if (next == close) return YES;
            if (next != ',') return NO;
        }
        return NO;
    }
    NSUInteger start = *i;
    while (*i < s.length && ![[NSCharacterSet characterSetWithCharactersInString:@",]} \r\n\t"] characterIsMember:[s characterAtIndex:*i]]) (*i)++;
    return *i > start;
}
static id JSON(NSData *data) {
    if (!data || data.length > Limit) return nil;
    NSString *s = [[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding];
    if (!s) return nil; NSUInteger index = 0;
    if (!Scan(s, &index, 0)) return nil; Space(s, &index); if (index != s.length) return nil;
    return [NSJSONSerialization JSONObjectWithData:data options:0 error:nil];
}
@interface BoundedHTTP : NSObject <NSURLSessionDataDelegate, NSURLSessionTaskDelegate>
@property(nonatomic, strong) NSMutableData *data;
@property(nonatomic, strong) NSHTTPURLResponse *response;
@property(nonatomic, strong) dispatch_semaphore_t done;
@property(nonatomic) BOOL failed;
@end
@implementation BoundedHTTP
- (void)URLSession:(NSURLSession *)session dataTask:(NSURLSessionDataTask *)task didReceiveResponse:(NSURLResponse *)response completionHandler:(void (^)(NSURLSessionResponseDisposition))completion {
    self.response = (NSHTTPURLResponse *)response;
    if (response.expectedContentLength > (long long)Limit) { self.failed = YES; completion(NSURLSessionResponseCancel); }
    else completion(NSURLSessionResponseAllow);
}
- (void)URLSession:(NSURLSession *)session dataTask:(NSURLSessionDataTask *)task didReceiveData:(NSData *)data {
    if (self.data.length + data.length > Limit) { self.failed = YES; [task cancel]; }
    else [self.data appendData:data];
}
- (void)URLSession:(NSURLSession *)session task:(NSURLSessionTask *)task willPerformHTTPRedirection:(NSHTTPURLResponse *)response newRequest:(NSURLRequest *)request completionHandler:(void (^)(NSURLRequest *))completion {
    self.failed = YES; completion(nil);
}
- (void)URLSession:(NSURLSession *)session task:(NSURLSessionTask *)task didCompleteWithError:(NSError *)error {
    if (error) self.failed = YES;
    dispatch_semaphore_signal(self.done);
}
@end
static NSDictionary *Request(NSString *endpoint, NSString *path, NSDictionary *payload, NSString *csrf, NSString *cookie, NSString **setCookie) {
    NSData *body = [NSJSONSerialization dataWithJSONObject:payload options:0 error:nil];
    if (!body || body.length > Limit) return nil;
    NSMutableURLRequest *request = [NSMutableURLRequest requestWithURL:[NSURL URLWithString:[endpoint stringByAppendingString:path]]];
    request.HTTPMethod = @"POST"; request.HTTPBody = body; request.timeoutInterval = 10;
    [request setValue:@"application/json" forHTTPHeaderField:@"Content-Type"];
    [request setValue:[NSUUID UUID].UUIDString forHTTPHeaderField:@"X-Tobkiri-Request-ID"];
    if (csrf.length) [request setValue:csrf forHTTPHeaderField:@"X-Rumi-CSRF"];
    if (cookie.length) [request setValue:cookie forHTTPHeaderField:@"Cookie"];
    NSURLSessionConfiguration *config = [NSURLSessionConfiguration ephemeralSessionConfiguration];
    config.connectionProxyDictionary = @{}; config.HTTPShouldSetCookies = NO; config.HTTPCookieStorage = nil;
    BoundedHTTP *delegate = [BoundedHTTP new]; delegate.data = [NSMutableData data]; delegate.done = dispatch_semaphore_create(0);
    NSURLSession *session = [NSURLSession sessionWithConfiguration:config delegate:delegate delegateQueue:nil];
    NSURLSessionDataTask *task = [session dataTaskWithRequest:request]; [task resume];
    BOOL timeout = dispatch_semaphore_wait(delegate.done, dispatch_time(DISPATCH_TIME_NOW, 12*NSEC_PER_SEC)) != 0;
    [session invalidateAndCancel];
    if (timeout || delegate.failed || delegate.response.statusCode < 200 || delegate.response.statusCode >= 300) return nil;
    if (setCookie) {
        NSArray *cookies = [NSHTTPCookie cookiesWithResponseHeaderFields:delegate.response.allHeaderFields forURL:request.URL];
        *setCookie = [NSHTTPCookie requestHeaderFieldsWithCookies:cookies][@"Cookie"] ?: @"";
    }
    id result = JSON(delegate.data); return IsDict(result) ? result : nil;
}
static NSData *ReadBoundedFD(int fd, NSUInteger max) {
    NSMutableData *data = [NSMutableData data]; unsigned char bytes[512];
    while (data.length <= max) {
        ssize_t n = read(fd, bytes, MIN(sizeof(bytes), max+1-data.length));
        if (n < 0) return nil; if (!n) return data;
        [data appendBytes:bytes length:(NSUInteger)n];
    }
    return nil;
}
static NSDictionary *Commands(void) {
    uint32_t size = 0; _NSGetExecutablePath(NULL, &size); char *buffer = calloc(size, 1);
    if (_NSGetExecutablePath(buffer, &size) != 0) { free(buffer); return nil; }
    NSString *binary = [NSString stringWithUTF8String:buffer]; free(buffer);
    NSString *contents = [[binary stringByDeletingLastPathComponent] stringByDeletingLastPathComponent];
    NSString *directory = [contents stringByAppendingPathComponent:@"Resources"];
    struct stat st;
    if (lstat(directory.fileSystemRepresentation, &st) || !S_ISDIR(st.st_mode)) return nil;
    NSString *path = [directory stringByAppendingPathComponent:@"commands.json"];
    int fd = open(path.fileSystemRepresentation, O_RDONLY | O_NOFOLLOW);
    if (fd < 0 || fstat(fd, &st) || !S_ISREG(st.st_mode)) { if (fd >= 0) close(fd); return nil; }
    NSData *data = ReadBoundedFD(fd, Limit); close(fd); if (!data) return nil;
    unsigned char digest[CC_SHA256_DIGEST_LENGTH]; CC_SHA256(data.bytes, (CC_LONG)data.length, digest);
    NSMutableString *hex = [NSMutableString string]; for (NSUInteger i=0; i<sizeof(digest); i++) [hex appendFormat:@"%02x", digest[i]];
    if (![hex isEqualToString:@COMMAND_DIGEST]) return nil;
    id commands = JSON(data); if (!Keys(commands, @[@"transcript.render"])) return nil;
    NSDictionary *decl = commands[@"transcript.render"];
    if (!Keys(decl, @[@"namespace", @"path", @"input_schema"]) || ![decl[@"namespace"] isEqual:@"acceptance.cli.application"] || ![decl[@"path"] isEqual:@"/api/transcript/render"]) return nil;
    return commands;
}
static NSUInteger Scalars(NSString *s) {
    NSUInteger count = 0;
    for (NSUInteger i=0; i<s.length; i++,count++) {
        unichar c = [s characterAtIndex:i];
        if (CFStringIsSurrogateHighCharacter(c) && i+1<s.length && CFStringIsSurrogateLowCharacter([s characterAtIndex:i+1])) i++;
    }
    return count;
}
static BOOL Input(id payload) {
    if (!Keys(payload, @[@"messages", @"columns", @"output_limit"]) || !Integer(payload[@"columns"],20,200) || !Integer(payload[@"output_limit"],1,Limit) || !IsArray(payload[@"messages"]) || [payload[@"messages"] count]>128) return NO;
    for (id message in payload[@"messages"]) {
        if (!Keys(message,@[@"role",@"text"]) || ![@[@"user",@"assistant",@"system",@"tool"] containsObject:message[@"role"]] || !IsString(message[@"text"]) || Scalars(message[@"text"])>8192) return NO;
    }
    return YES;
}
static BOOL Frame(id frame) {
    NSArray *required = @[@"protocol",@"type",@"request_id",@"command",@"arguments",@"stdin",@"tty",@"output_limit"];
    NSMutableSet *allowed = [NSMutableSet setWithArray:required]; [allowed addObjectsFromArray:@[@"cancel",@"signal"]];
    if (!IsDict(frame) || ![[NSSet setWithArray:[frame allKeys]] isSubsetOfSet:allowed]) return NO;
    for (NSString *key in required) if (!frame[key]) return NO;
    if (![frame[@"protocol"] isEqual:@"io.tobkiri.cli.io.v1"] || ![frame[@"type"] isEqual:@"command"] || ![frame[@"command"] isEqual:@"application.invoke"] || !Match(frame[@"request_id"],@"^cli:req:[a-z0-9][a-z0-9._-]{7,127}$") || frame[@"stdin"] != [NSNull null] || CFGetTypeID((__bridge CFTypeRef)frame[@"tty"]) != CFBooleanGetTypeID() || [frame[@"tty"] boolValue] || !Integer(frame[@"output_limit"],1,Limit)) return NO;
    if (frame[@"cancel"] && CFGetTypeID((__bridge CFTypeRef)frame[@"cancel"]) != CFBooleanGetTypeID()) return NO;
    if (frame[@"signal"] && ![@[@"SIGINT",@"SIGTERM",@"SIGHUP"] containsObject:frame[@"signal"]]) return NO;
    return Keys(frame[@"arguments"],@[@"command_id",@"input"]) && [frame[@"arguments"][@"command_id"] isEqual:@"transcript.render"] && Input(frame[@"arguments"][@"input"]);
}
static NSDictionary *ErrorFrame(NSString *requestID) {
    return @{@"protocol":@"io.tobkiri.cli.io.v1",@"type":@"error",@"request_id":requestID,@"error":GenericError,@"exit_status":@64};
}
static NSDictionary *Invoke(id frame, NSString *endpoint, NSString *csrf, NSString *cookie) {
    NSString *requestID = Match(frame[@"request_id"],@"^cli:req:[a-z0-9][a-z0-9._-]{7,127}$") ? frame[@"request_id"] : @"cli:req:invalid000";
    if (!Frame(frame)) return ErrorFrame(requestID);
    NSDictionary *value;
    if ([frame[@"cancel"] boolValue] || frame[@"signal"]) value = @{@"stdout":@"",@"stderr":@"request cancelled",@"exit_status":@130,@"stream":@"cancelled"};
    else {
        NSDictionary *envelope = Request(endpoint,@"/api/contracts/acceptance.cli.application/POST%20%2Fapi%2Ftranscript%2Frender",frame[@"arguments"][@"input"],csrf,cookie,NULL);
        if (!True(envelope[@"success"]) || !IsDict(envelope[@"data"]) || ![envelope[@"data"][@"status"] isEqual:@"ok"]) return ErrorFrame(requestID);
        value = envelope[@"data"][@"value"];
    }
    if (!Keys(value,@[@"stdout",@"stderr",@"exit_status",@"stream"]) || !IsString(value[@"stdout"]) || !IsString(value[@"stderr"]) || !Integer(value[@"exit_status"],0,255) || ![@[@"complete",@"cancelled"] containsObject:value[@"stream"]] || [[value[@"stdout"] stringByAppendingString:value[@"stderr"]] lengthOfBytesUsingEncoding:NSUTF8StringEncoding] > [frame[@"output_limit"] unsignedIntegerValue]) return ErrorFrame(requestID);
    NSMutableDictionary *result = [value mutableCopy]; result[@"protocol"]=@"io.tobkiri.cli.io.v1"; result[@"type"]=@"result"; result[@"request_id"]=requestID; return result;
}
int main(int argc, char **argv) {
    @autoreleasepool {
        if (argc != 3 || strcmp(argv[1],"--bootstrap-fd")) { fputs("Tobkiri CLI requires an inherited bootstrap pipe\n",stderr); return 64; }
        char *end; long raw = strtol(argv[2],&end,10); struct stat st;
        if (*end || raw < 3 || raw > INT_MAX || fstat((int)raw,&st) || !S_ISFIFO(st.st_mode)) { fputs("Tobkiri CLI bootstrap denied or unavailable\n",stderr); return 64; }
        NSDictionary *commands = Commands(); id bootstrap = JSON(ReadBoundedFD((int)raw,2048));
        if (!commands || !Keys(bootstrap,@[@"endpoint",@"bootstrap_code"]) || !Match(bootstrap[@"endpoint"],@"^http://127\\.0\\.0\\.1:[0-9]{1,5}/?$") || !IsString(bootstrap[@"bootstrap_code"]) || [bootstrap[@"bootstrap_code"] length]<1 || [bootstrap[@"bootstrap_code"] length]>512) { fputs("Tobkiri CLI bootstrap denied or unavailable\n",stderr); return 64; }
        NSString *endpoint = bootstrap[@"endpoint"]; if ([endpoint hasSuffix:@"/"]) endpoint=[endpoint substringToIndex:endpoint.length-1];
        NSURLComponents *url = [NSURLComponents componentsWithString:endpoint]; if (url.port.integerValue<1 || url.port.integerValue>65535) return 64;
        NSString *cookie = @"";
        NSDictionary *envelope = Request(endpoint,@"/api/panel/auth/exchange",@{@"code":bootstrap[@"bootstrap_code"]},@"",@"",&cookie);
        NSDictionary *session = envelope[@"data"];
        if (!True(envelope[@"success"]) || !IsDict(session) || !IsString(session[@"csrf_token"]) || ![session[@"csrf_token"] length] || !IsString(session[@"journal_scope"]) || ![session[@"journal_scope"] length]) { fputs("Tobkiri CLI bootstrap denied or unavailable\n",stderr); return 64; }
        while (YES) {
            @autoreleasepool {
                NSMutableData *line = [NSMutableData data]; BOOL overflow = NO; int ch;
                while ((ch=fgetc(stdin)) != EOF && ch!='\n') { if (line.length<Limit) { unsigned char byte=(unsigned char)ch; [line appendBytes:&byte length:1]; } else overflow=YES; }
                if (ch==EOF && !line.length && !overflow) break;
                id frame = overflow ? nil : JSON(line);
                NSDictionary *result = IsDict(frame) ? Invoke(frame,endpoint,session[@"csrf_token"],cookie) : ErrorFrame(@"cli:req:invalid000");
                NSData *data = [NSJSONSerialization dataWithJSONObject:result options:NSJSONWritingSortedKeys error:nil];
                fwrite(data.bytes,1,data.length,stdout); fputc('\n',stdout); fflush(stdout);
            }
        }
        return 0;
    }
}
