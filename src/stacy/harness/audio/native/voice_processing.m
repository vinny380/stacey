#import <AVFoundation/AVFoundation.h>
#import <Foundation/Foundation.h>

static dispatch_queue_t writer;
static AVAudioEngine *engine;
static AVAudioPlayerNode *player;
static NSInteger epoch = 0;

static void sendMessage(NSDictionary *message) {
    dispatch_async(writer, ^{
        NSData *bytes = [NSJSONSerialization dataWithJSONObject:message options:0 error:nil];
        if (bytes) {
            [[NSFileHandle fileHandleWithStandardOutput] writeData:bytes];
            [[NSFileHandle fileHandleWithStandardOutput] writeData:[NSData dataWithBytes:"\n" length:1]];
        }
    });
}

static void fail(NSString *message) {
    sendMessage(@{@"type": @"error", @"message": message});
    dispatch_sync(writer, ^{});
    exit(1);
}

int main(int argc, const char **argv) {
    @autoreleasepool {
        writer = dispatch_queue_create("stacy.audio.messages", DISPATCH_QUEUE_SERIAL);
        engine = [[AVAudioEngine alloc] init];
        player = [[AVAudioPlayerNode alloc] init];
        [engine attachNode:player];
        AVAudioFormat *render = [[AVAudioFormat alloc] initStandardFormatWithSampleRate:24000 channels:1];
        [engine connect:player to:engine.mainMixerNode format:render];
        // Build the output graph before enabling the shared voice-processing I/O unit.
        NSError *error = nil;
        if (![engine.inputNode setVoiceProcessingEnabled:YES error:&error])
            fail([@"enable voice processing: " stringByAppendingString:error.localizedDescription ?: @"unavailable"]);
        if (!engine.inputNode.isVoiceProcessingEnabled || !engine.outputNode.isVoiceProcessingEnabled)
            fail(@"Native echo cancellation is unavailable on selected audio devices");
        engine.inputNode.voiceProcessingBypassed = NO;
        engine.inputNode.voiceProcessingAGCEnabled = YES;
        if (@available(macOS 14.0, *)) {
            AVAudioVoiceProcessingOtherAudioDuckingConfiguration ducking = {NO, AVAudioVoiceProcessingOtherAudioDuckingLevelMin};
            engine.inputNode.voiceProcessingOtherAudioDuckingConfiguration = ducking;
        }
        AVAudioFormat *source = [[AVAudioFormat alloc] initStandardFormatWithSampleRate:[engine.inputNode outputFormatForBus:0].sampleRate channels:1];
        AVAudioFormat *pcmFormat = [[AVAudioFormat alloc] initWithCommonFormat:AVAudioPCMFormatInt16
                                          sampleRate:24000 channels:1 interleaved:YES];
        AVAudioConverter *converter = [[AVAudioConverter alloc] initFromFormat:source toFormat:pcmFormat];
        if (!converter || source.commonFormat != AVAudioPCMFormatFloat32 || source.isInterleaved)
            fail(@"Unsupported native microphone format");
        dispatch_semaphore_t slots = dispatch_semaphore_create(8);
        [engine.inputNode installTapOnBus:0 bufferSize:480 format:source block:^(AVAudioPCMBuffer *input, AVAudioTime *when) {
            if (dispatch_semaphore_wait(slots, DISPATCH_TIME_NOW) != 0) return;
            AVAudioPCMBuffer *copy = [[AVAudioPCMBuffer alloc] initWithPCMFormat:source frameCapacity:input.frameLength];
            copy.frameLength = input.frameLength;
            for (int ch = 0; ch < source.channelCount; ch++)
                memcpy(copy.floatChannelData[ch], input.floatChannelData[ch], input.frameLength * sizeof(float));
            dispatch_async(writer, ^{
                @autoreleasepool {
                    AVAudioFrameCount capacity = ceil(copy.frameLength * 24000 / source.sampleRate) + 32;
                    AVAudioPCMBuffer *output = [[AVAudioPCMBuffer alloc] initWithPCMFormat:pcmFormat frameCapacity:capacity];
                    __block BOOL offered = NO;
                    NSError *conversionError = nil;
                    AVAudioConverterOutputStatus status = [converter convertToBuffer:output error:&conversionError
                        withInputFromBlock:^AVAudioBuffer *(AVAudioPacketCount count, AVAudioConverterInputStatus *status) {
                            if (offered) { *status = AVAudioConverterInputStatus_NoDataNow; return nil; }
                            offered = YES; *status = AVAudioConverterInputStatus_HaveData; return copy;
                        }];
                    if (status == AVAudioConverterOutputStatus_Error) {
                        dispatch_async(dispatch_get_main_queue(), ^{ fail(@"Microphone conversion failed"); });
                    } else if (output.frameLength > 0) {
                        NSData *pcm = [NSData dataWithBytes:output.int16ChannelData[0] length:output.frameLength * 2];
                        NSDictionary *message = @{@"type": @"audio", @"pcm": [pcm base64EncodedStringWithOptions:0]};
                        NSData *bytes = [NSJSONSerialization dataWithJSONObject:message options:0 error:nil];
                        [[NSFileHandle fileHandleWithStandardOutput] writeData:bytes];
                        [[NSFileHandle fileHandleWithStandardOutput] writeData:[NSData dataWithBytes:"\n" length:1]];
                    }
                    dispatch_semaphore_signal(slots);
                }
            });
        }];
        [engine prepare];
        if (![engine startAndReturnError:&error]) fail([@"start engine: " stringByAppendingString:error.localizedDescription ?: @"failed"]);
        [player play];
        [[NSNotificationCenter defaultCenter] addObserverForName:AVAudioEngineConfigurationChangeNotification
            object:engine queue:[NSOperationQueue mainQueue] usingBlock:^(NSNotification *notification) {
                // A device change can invalidate the verified capture/render reference.
                fail(@"Audio device changed; restart voice to verify echo cancellation again");
            }];
        sendMessage(@{@"type": @"ready", @"echo_cancelled": @YES, @"sample_rate": @24000});
        dispatch_async(dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0), ^{
            char *line = NULL; size_t size = 0;
            while (getline(&line, &size, stdin) != -1) {
                @autoreleasepool {
                    NSData *data = [[NSString stringWithUTF8String:line] dataUsingEncoding:NSUTF8StringEncoding];
                    NSDictionary *message = [NSJSONSerialization JSONObjectWithData:data options:0 error:nil];
                    if (![message isKindOfClass:[NSDictionary class]]) fail(@"Invalid audio command");
                    dispatch_async(dispatch_get_main_queue(), ^{
                        NSString *type = message[@"type"];
                        if ([type isEqual:@"play"]) {
                            NSNumber *identifier = message[@"id"];
                            NSData *bytes = [[NSData alloc] initWithBase64EncodedString:message[@"pcm"] options:0];
                            if (!identifier || !bytes.length || bytes.length % 2 || bytes.length > 48000)
                                fail(@"Invalid playback PCM");
                            NSInteger generation = epoch;
                            AVAudioPCMBuffer *buffer = [[AVAudioPCMBuffer alloc] initWithPCMFormat:render frameCapacity:(AVAudioFrameCount)bytes.length / 2];
                            buffer.frameLength = (AVAudioFrameCount)bytes.length / 2;
                            const int16_t *samples = bytes.bytes;
                            for (int index = 0; index < buffer.frameLength; index++)
                                buffer.floatChannelData[0][index] = samples[index] / 32768.0f;
                            if (!player.isPlaying) [player play];
                            [player scheduleBuffer:buffer completionCallbackType:AVAudioPlayerNodeCompletionDataPlayedBack
                                completionHandler:^(AVAudioPlayerNodeCompletionCallbackType type) {
                                    dispatch_async(dispatch_get_main_queue(), ^{
                                        if (epoch == generation) sendMessage(@{@"type": @"played", @"id": identifier});
                                    });
                                }];
                        } else if ([type isEqual:@"clear"]) {
                            epoch++; [player stop]; sendMessage(@{@"type": @"cleared", @"id": message[@"id"]});
                        } else if ([type isEqual:@"stop"]) {
                            [engine.inputNode removeTapOnBus:0]; [player stop]; [engine stop]; exit(0);
                        } else fail(@"Unknown audio command");
                    });
                }
            }
            free(line);
            dispatch_async(dispatch_get_main_queue(), ^{
                [engine.inputNode removeTapOnBus:0]; [player stop]; [engine stop]; exit(0);
            });
        });
        [[NSRunLoop mainRunLoop] run];
    }
    return 0;
}
